#!/usr/bin/env python3
"""
media.py
--------
Fetch the field photos that the tables only *reference*, so an output directory
is self-contained and the HTML report renders without credentials.

WHY THIS EXISTS
---------------
Observation values for image/video questions are absolute S3 URLs::

    https://s3.ap-south-1.amazonaws.com/prod-user-media/pumpOperatorUat/<uuid>.jpg

That bucket denies anonymous reads (an unauthenticated GET returns
``<Code>AccessDenied</Code>``), so a report shared on Box shows broken images for
everyone -- including the person who generated it, once their browser session is
gone. Avni brokers access through an authenticated endpoint that mints a
short-lived presigned URL::

    GET /media/signedUrl?url=<the s3 url>     ->  200, presigned URL as plain text

The presigned URL expires, so it is fetched and used immediately and never
stored. What gets stored is the downloaded BYTES.

WHAT IT DOES
------------
  1. scan the CSV tables for media URLs,
  2. resolve each one to a presigned URL and download it into ``<out>/media/``,
  3. rewrite ``report.html`` so its embedded data points at ``media/<file>``,
  4. write ``media/_media_manifest.csv`` mapping every original URL to its local
     file, with size + sha256.

The CSVs keep the canonical S3 URLs: they are the stable identifiers, and the
manifest is the join back to the local copies. Only the report is rewritten,
because only the report needs to render offline.

Downloads are idempotent -- an existing file of nonzero size is skipped -- so a
re-run after a partial failure only fetches what is missing.

USAGE
-----
    from avni_auth import AvniClient
    from avni_pipeline import media

    CLEAN  = "1-data/12-avni/121-clean"
    client = AvniClient()
    urls   = media.collect_urls(CLEAN)
    result = media.download_all(client, urls, CLEAN + "/media", progress=print)
    media.rewrite_report(CLEAN + "/report.html", result.local_by_url)

Entry point: ../../21-wrangling/fetch_media.py
"""

import concurrent.futures
import csv
import hashlib
import os
import re
import urllib.error
import urllib.parse
import urllib.request

# Hosts/paths that indicate Avni-managed user media. Kept deliberately narrow so
# a stray http link in a free-text answer is never mistaken for a photo.
MEDIA_HOST_RE = re.compile(r"^https?://[^/]*\bs3[.-][^/]*\bamazonaws\.com/", re.I)
MEDIA_PATH_RE = re.compile(r"/(?:prod|staging|uat)-user-media/", re.I)
MEDIA_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic",
             ".mp4", ".mov", ".3gp", ".m4a", ".mp3", ".pdf"}

URL_RE = re.compile(r"https?://[^\s,\"'|<>\\]+")
DEFAULT_WORKERS = 6
MANIFEST_NAME = "_media_manifest.csv"


class MediaResult(object):
    """Outcome of a download pass (counts + the url -> local path mapping)."""

    def __init__(self):
        self.local_by_url = {}      # original url -> path relative to the out dir
        self.downloaded = 0
        self.skipped = 0            # already present on disk
        self.failed = {}            # url -> error string
        self.bytes = 0
        self.records = []           # manifest rows

    def summary(self):
        return (f"{self.downloaded} downloaded, {self.skipped} already present, "
                f"{len(self.failed)} failed, {self.bytes / 1e6:.1f} MB")


def is_media_url(url):
    """True for an Avni user-media URL we should localise."""
    if not isinstance(url, str) or not MEDIA_HOST_RE.match(url):
        return False
    path = urllib.parse.urlparse(url).path
    if not MEDIA_PATH_RE.search(path):
        return False
    return os.path.splitext(path)[1].lower() in MEDIA_EXT


def collect_urls(out_dir):
    """Every distinct media URL referenced by the CSV tables in ``out_dir``.

    Scans all CSVs rather than a hardcoded column list: image questions can live
    in observations_long, in a fact table, or on the subject, and the set of
    media-bearing concepts changes as forms change.
    """
    found = set()
    for name in sorted(os.listdir(out_dir)):
        if not name.lower().endswith(".csv"):
            continue
        with open(os.path.join(out_dir, name), encoding="utf-8", newline="") as fh:
            for row in csv.reader(fh):
                for cell in row:
                    if "http" not in cell:
                        continue
                    for candidate in URL_RE.findall(cell):
                        if is_media_url(candidate):
                            found.add(candidate)
    return sorted(found)


def local_name(url):
    """Local filename for a media URL: the S3 key's basename (already a uuid)."""
    base = os.path.basename(urllib.parse.urlparse(url).path)
    return urllib.parse.unquote(base) or hashlib.sha1(
        url.encode("utf-8")).hexdigest() + ".bin"


def signed_url(client, url, timeout=30):
    """Resolve an S3 URL to a short-lived presigned URL via Avni.

    Returns plain text, not JSON, so it bypasses ``AvniClient.get``; the auth
    header and token refresh still come from the client.
    """
    endpoint = (client.server + "/media/signedUrl?"
                + urllib.parse.urlencode({"url": url}))
    req = urllib.request.Request(
        endpoint, headers={"auth-token": client.token(), "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read().decode("utf-8").strip()


S3_ERROR_CODE_RE = re.compile(r"<Code>([^<]+)</Code>")

# The signed-URL endpoint mints a URL for ANY key without checking that the
# object exists, so a missing photo only surfaces as NoSuchKey on the S3 GET.
# Distinguishing the two stages is what separates "our auth is broken" from
# "this file was never uploaded" -- they need very different responses.
MISSING_FROM_BUCKET = "missing from bucket (S3 NoSuchKey)"


def _s3_error(err):
    """Turn an S3 error response into a short reason string."""
    try:
        body = err.read().decode("utf-8", "replace")
    except Exception:                             # noqa: BLE001
        body = ""
    match = S3_ERROR_CODE_RE.search(body)
    code = match.group(1) if match else ""
    if code == "NoSuchKey":
        return MISSING_FROM_BUCKET
    return f"S3 HTTP {err.code}" + (f" ({code})" if code else "")


def _fetch_one(client, url, media_dir, timeout):
    """Resolve + download one file. Returns a record dict (never raises)."""
    name = local_name(url)
    dest = os.path.join(media_dir, name)
    rel = os.path.join(os.path.basename(media_dir), name)

    def record(status, nbytes=0, digest="", path=""):
        return {"s3_url": url, "local_path": path, "bytes": nbytes,
                "sha256": digest, "status": status}

    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        with open(dest, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        return record("present", os.path.getsize(dest), digest, rel)

    try:
        presigned = signed_url(client, url, timeout=timeout)
    except urllib.error.HTTPError as err:
        return record(f"signedUrl HTTP {err.code}")
    except Exception as err:                      # noqa: BLE001 - report, never abort
        return record(f"signedUrl {type(err).__name__}: {err}")

    if not presigned.lower().startswith("http"):
        return record(f"signedUrl returned {presigned[:60]!r}")

    try:
        with urllib.request.urlopen(presigned, timeout=timeout) as response:
            payload = response.read()
    except urllib.error.HTTPError as err:
        return record(_s3_error(err))
    except Exception as err:                      # noqa: BLE001
        return record(f"download {type(err).__name__}: {err}")

    if not payload:
        return record("empty response body")

    tmp = dest + ".part"
    with open(tmp, "wb") as fh:
        fh.write(payload)
    os.replace(tmp, dest)                         # atomic: no half files on Box
    return record("downloaded", len(payload),
                  hashlib.sha256(payload).hexdigest(), rel)


def download_all(client, urls, media_dir, workers=DEFAULT_WORKERS,
                 timeout=30, progress=None):
    """Download every URL into ``media_dir``. Failures are recorded, not raised."""
    os.makedirs(media_dir, exist_ok=True)
    result = MediaResult()
    if not urls:
        return result

    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_fetch_one, client, u, media_dir, timeout)
                   for u in urls]
        for future in concurrent.futures.as_completed(futures):
            rec = future.result()
            done += 1
            result.records.append(rec)            # every url, success or not
            status = rec["status"]
            if rec["local_path"]:
                result.local_by_url[rec["s3_url"]] = rec["local_path"]
                result.bytes += rec["bytes"]
                if status == "downloaded":
                    result.downloaded += 1
                else:
                    result.skipped += 1
                note = f"{rec['bytes'] / 1024:.0f} KB"
            else:
                result.failed[rec["s3_url"]] = status
                note = "FAILED " + status
            if progress and (done % 25 == 0 or done == len(urls)):
                progress(f"  [{done}/{len(urls)}] {local_name(rec['s3_url'])}  {note}")
    return result


def write_manifest(result, media_dir):
    """Write media/_media_manifest.csv: the join from S3 URL to local copy.

    Every referenced URL gets a row, including the ones that could not be
    fetched -- a photo that is referenced but absent from the bucket is a
    finding about the data, so it belongs in the record rather than only in
    console output that scrolls away.
    """
    path = os.path.join(media_dir, MANIFEST_NAME)
    rows = sorted(result.records, key=lambda r: (not r["local_path"], r["s3_url"]))
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=["s3_url", "local_path", "bytes", "sha256", "status"])
        writer.writeheader()
        writer.writerows(rows)
    return path


def rewrite_report(report_path, local_by_url):
    """Point the report's embedded data at the local copies.

    A plain string replacement over the whole file: the URLs are opaque absolute
    strings inside the JSON blob, so this touches nothing else. URLs that failed
    to download keep their S3 value (a broken image beats a dangling relative
    link that hides the failure).
    """
    if not os.path.exists(report_path) or not local_by_url:
        return 0
    with open(report_path, encoding="utf-8") as fh:
        html = fh.read()
    replaced = 0
    # Longest first, so no URL that is a prefix of another is partially rewritten.
    for url in sorted(local_by_url, key=len, reverse=True):
        if url in html:
            html = html.replace(url, local_by_url[url])
            replaced += 1
    tmp = report_path + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(html)
    os.replace(tmp, report_path)
    return replaced
