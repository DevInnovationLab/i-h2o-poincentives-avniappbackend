#!/usr/bin/env python3
"""
pull.py
-------
The API-reading layer: everything that talks to Avni. It uses
``avni_auth.AvniClient`` for auth, token refresh and cursor pagination -- no
hand-rolled urllib, no static token (a full-org pull can outlive the ~1h id
token, so refresh has to be automatic).

THE ONE RULE THAT MATTERS
-------------------------
The server does NOT honour ``/api/encounters?subjectId=`` -- it happily returns
org-wide encounters regardless of the id you send. So this module pulls every
general encounter ONCE and attributes each one client-side by its own
``"Subject ID"`` back-reference, falling back to the subject document's
``encounters[]`` uuid list for records with a blank ``Subject ID``. Never trust
the server-side filter (doing so once reported 99 encounters for a pump whose
true count is 8).

Output shape (``save_raw`` writes it and ``load_raw`` replays it verbatim, so
the rest of the pipeline does not care where the data came from)::

    {
      "meta":              {source, server, subject_type, since, version, pulled_at},
      "subjects":          [<full subject doc>, ...],
      "encounters":        [<encounter> + _subject_id/_attribution, ...],
      "enrolments":        [<programEnrolment>, ...],
      "program_encounters":[<programEncounter> + _subject_id, ...],
      "attribution":       {counts used by the QC report},
    }

USAGE
-----
    from avni_auth import AvniClient
    from avni_pipeline import pull

    client = AvniClient()                       # env: AVNI_AUTH_TOKEN or AVNI_USERNAME/PASSWORD
    raw = pull.pull_all(client, subject_type="Pump")
    pull.save_raw(raw, "1-data/12-avni/120-raw/_raw.json")   # replayable snapshot
"""

import datetime
import json
import os
import sys

# avni_auth.py sits next to this package, not inside it: dirname twice lands on
# 2-code/20-programs/, the import root. Adding it here means the package still
# finds the client when it is imported directly, not only via the 21-wrangling
# CLIs that put that directory on the path themselves.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from avni_auth import AvniClient, AvniError, decode_jwt_claims  # noqa: E402

DEFAULT_SINCE = "1900-01-01T00:00:00.001Z"


def _log(progress, message):
    if progress:
        progress(message)


# --------------------------------------------------------------------------- #
# The client-side subject <-> encounter join  (ported from a predecessor
# script, no longer in this repository)
# --------------------------------------------------------------------------- #

def build_encounter_index(encounters):
    """Index an org-wide encounter pull for attribution.

    Returns ``(by_subject, by_id)``: ``{Subject ID: [encounter, ...]}`` and
    ``{ID: encounter}``. Built once per run; the per-subject server filter is
    unreliable so this index -- not the API -- decides ownership.
    """
    by_subject, by_id = {}, {}
    for e in encounters:
        by_subject.setdefault(e.get("Subject ID"), []).append(e)
        if e.get("ID"):
            by_id[e["ID"]] = e
    return by_subject, by_id


def subject_encounters(subject, index):
    """This subject's general encounters, joined authoritatively.

    Authoritative source is each encounter's own ``"Subject ID"``; the subject
    document's ``encounters[]`` uuid list is only a fallback for encounters
    whose ``Subject ID`` is missing/blank. An encounter whose back-reference
    names a DIFFERENT subject is never claimed by the fallback -- the encounter's
    own field wins over a stale array. Returns ``(encounters, n_fallback,
    n_conflicts)``.
    """
    by_subject, by_id = index
    sid = subject.get("ID")
    result = list(by_subject.get(sid, []))
    have = {e.get("ID") for e in result}
    fallback = conflicts = 0
    for uid in (subject.get("encounters") or []):
        if uid in have or uid not in by_id:
            continue
        candidate = by_id[uid]
        owner = candidate.get("Subject ID")
        if owner and owner != sid:
            conflicts += 1          # listed here but owned there: trust the owner
            continue
        result.append(candidate)
        have.add(uid)
        fallback += 1
    return result, fallback, conflicts


def attribute_encounters(subjects, encounters, progress=None):
    """Attach every encounter to exactly one subject, client-side.

    Tags each attributed encounter with ``_subject_id`` and ``_attribution``
    ('subject-id' or 'subject-doc-fallback'). Returns
    ``(attributed, stats)``; encounters that match no pulled subject are counted
    in ``stats['unattributed']`` and listed in ``stats['unattributed_ids']``
    rather than being dropped quietly.
    """
    index = build_encounter_index(encounters)
    attributed, seen, fallback_total = [], set(), 0
    conflict_total, duplicates = 0, []
    for subject in subjects:
        own, n_fallback, n_conflicts = subject_encounters(subject, index)
        fallback_total += n_fallback
        conflict_total += n_conflicts
        for e in own:
            eid = e.get("ID")
            if eid in seen:
                duplicates.append(eid)
                continue
            seen.add(eid)
            tagged = dict(e)
            tagged["_subject_id"] = subject.get("ID")
            tagged["_attribution"] = ("subject-id"
                                      if e.get("Subject ID") == subject.get("ID")
                                      else "subject-doc-fallback")
            attributed.append(tagged)
    unattributed = [e.get("ID") for e in encounters if e.get("ID") not in seen]
    stats = {
        "pulled": len(encounters),
        "attributed": len(attributed),
        "by_subject_id": len(attributed) - fallback_total,
        "by_subject_doc_fallback": fallback_total,
        "unattributed": len(unattributed),
        "unattributed_ids": unattributed[:50],
        "claimed_by_multiple_subjects": len(duplicates),
        "subject_doc_conflicts": conflict_total,
    }
    _log(progress, f"  attributed {stats['attributed']}/{stats['pulled']} encounters "
                   f"({stats['by_subject_doc_fallback']} via subject-doc fallback, "
                   f"{stats['unattributed']} unattributed)")
    return attributed, stats


# --------------------------------------------------------------------------- #
# The pull
# --------------------------------------------------------------------------- #

def pull_subjects(client, subject_type=None, since=None, progress=None):
    """List subjects, then fetch each one's FULL document.

    The list endpoint omits ``encounters[]``/``enrolments[]``; the per-subject
    GET is what carries them (and the full address hierarchy).
    """
    listing = client.get_all_pages("/api/subjects", subjectType=subject_type,
                                   lastModifiedDateTime=since or DEFAULT_SINCE)
    _log(progress, f"Listed {len(listing)} subject(s)"
                   f"{f' of type {subject_type}' if subject_type else ''}.")
    full, failed = [], []
    for i, item in enumerate(listing, 1):
        sid = item.get("ID")
        if not sid:
            continue
        try:
            full.append(client.get(f"/api/subject/{sid}"))
        except AvniError as err:
            failed.append((sid, str(err).splitlines()[0]))
            _log(progress, f"  [{i}/{len(listing)}] {sid}: FAILED {str(err).splitlines()[0]}")
    if failed:
        _log(progress, f"  {len(failed)} subject document(s) failed to fetch.")
    _log(progress, f"Fetched {len(full)} full subject document(s).")
    return full, failed


def pull_encounters(client, since=None, progress=None):
    """ONE org-wide pull of general encounters (never per-subject: see module docstring)."""
    encounters = client.get_all_pages("/api/encounters",
                                      lastModifiedDateTime=since or DEFAULT_SINCE)
    _log(progress, f"Pulled {len(encounters)} general encounter(s) org-wide.")
    return encounters


def pull_programs(client, subjects, progress=None):
    """Walk each subject's ``enrolments[]`` -> enrolment doc -> program encounters.

    Written generically; for the Pump subject type there are no programs, so
    both lists come back empty. ``/api/programEnrolments`` needs the subject's
    External ID *and* a program name, hence the walk by uuid instead.
    """
    enrolments, program_encounters = [], []
    for subject in subjects:
        for enr_id in (subject.get("enrolments") or []):
            try:
                enr = client.get(f"/api/programEnrolment/{enr_id}")
            except AvniError as err:
                _log(progress, f"  enrolment {enr_id}: FAILED {str(err).splitlines()[0]}")
                continue
            enr = dict(enr)
            enr["_subject_id"] = subject.get("ID")
            enrolments.append(enr)
            try:
                pencs = client.get_all_pages("/api/programEncounters",
                                             programEnrolmentId=enr_id)
            except AvniError as err:
                _log(progress, f"  programEncounters for {enr_id}: FAILED "
                               f"{str(err).splitlines()[0]}")
                continue
            for pe in pencs:
                pe = dict(pe)
                pe["_subject_id"] = pe.get("Subject ID") or subject.get("ID")
                pe["_enrolment_id"] = enr_id
                pe["_attribution"] = "enrolment-walk"
                program_encounters.append(pe)
    if enrolments or program_encounters:
        _log(progress, f"Pulled {len(enrolments)} enrolment(s) and "
                       f"{len(program_encounters)} program encounter(s).")
    return enrolments, program_encounters


def pull_all(client, subject_type=None, since=None, progress=print):
    """Full pull: subjects + their encounters + programs, joined client-side.

    Order matters (fewest calls, and it applies the fix):
      1. list subjects, 2. fetch each full subject doc, 3. ONE org-wide
      encounter pull, 4. attribute client-side, 5. walk enrolments.
    """
    started = datetime.datetime.now(datetime.timezone.utc)
    subjects, failed = pull_subjects(client, subject_type, since, progress)
    encounters = pull_encounters(client, since, progress)
    attributed, stats = attribute_encounters(subjects, encounters, progress)
    enrolments, program_encounters = pull_programs(client, subjects, progress)

    claims = decode_jwt_claims(client._token) or {}
    meta = {
        "source": "api",
        "server": client.server,
        "subject_type": subject_type or "(all)",
        "since": since or DEFAULT_SINCE,
        "api_version": client.version,
        "pulled_at": started.isoformat(timespec="seconds"),
        "pull_seconds": round(
            (datetime.datetime.now(datetime.timezone.utc) - started).total_seconds(), 1),
        "token_user": claims.get("cognito:username") or claims.get("email") or "",
        "subject_fetch_failures": len(failed),
    }
    return {
        "meta": meta,
        "subjects": subjects,
        "encounters": attributed,
        "enrolments": enrolments,
        "program_encounters": program_encounters,
        "attribution": stats,
    }


# --------------------------------------------------------------------------- #
# Raw snapshots (replay a pull offline; useful for debugging counts)
# --------------------------------------------------------------------------- #

def save_raw(raw, path):
    """Write the raw dataset as JSON (exact API payloads, nothing transformed)."""
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(raw, fh, ensure_ascii=False, indent=1, sort_keys=False, default=str)
    return path


def load_raw(path):
    """Read back a snapshot written by save_raw()."""
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def make_client(**kwargs):
    """AvniClient factory so callers need not import avni_auth themselves."""
    return AvniClient(**kwargs)
