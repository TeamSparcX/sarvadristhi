# Standard Operating Procedure: DVR/NVR evidence, seizure to report

Version 0.1 · applies to Sarvadristhi 0.1.0 · for trained digital forensic examiners

This SOP covers surveillance recorders (DVR/NVR) of any brand. Follow it in order; record
every step in the case notes. Sarvadristhi writes its own steps to a hash-chained custody ledger,
but it cannot record what happens before the evidence reaches it.

## 1. At the scene, before touching the recorder

1. Photograph the recorder in place: front, rear, all cables, labels, serial plate.
2. Photograph the monitor if it is on, including the on-screen clock, next to a trusted
   reference clock (phone with network time) in the same frame. **Record the difference.**
   This recorder-clock offset is needed later to convert recorder time to real time.
3. Note make, model, serial number, number of channels, attached cameras and their
   locations. Note who had access and who handed the device over.
4. Decide: **live** acquisition (recorder stays running) or **dead** acquisition (disk
   removed). Default to dead. Choose live only when the device cannot be removed, the disk
   is encrypted at rest, or overwriting is imminent and removal would take too long.

## 2. Live acquisition (recorder running)

1. Connect the forensic laptop to the recorder's network only (no internet uplink).
2. Run `python -m sarvadristhi live --host <ip> --user <u> --password <p> --channels 1,2,... --start
   "<YYYY-MM-DD HH:MM:SS>" --end "<...>" --case <case dir>`.
   Sarvadristhi identifies the device over ONVIF, records its clock against the laptop clock, lists
   recordings, then downloads the requested window through the vendor API. Each file is
   hashed and logged.
3. Download a window wider than the incident (at least 30 minutes either side).
4. Do not change any setting on the recorder. Do not log in through the recorder's own UI
   unless unavoidable; if you must, photograph each screen.
5. Then proceed to dead acquisition if the device can be seized.

## 3. Dead acquisition (disk removed)

1. Power the recorder down normally if it is safe to do so; otherwise disconnect power.
   Note which, and the time.
2. Remove the hard disk(s). Label each with case number, slot position and serial.
3. Connect the disk through a **hardware write-blocker**. Software read-only opening is
   advisory: the operating system can still write to an attached disk.
4. Image: `python -m sarvadristhi acquire /dev/sdX <case>/evidence --case <case> --format e01`.
   E01 stores MD5 and SHA-256 inside the image and is verified with `ewfverify`.
   For a raw image use `--format raw` (Sarvadristhi hashes source and image and logs unreadable
   sectors).
5. Store the original disk sealed, with its exhibit label, in evidence storage. All further
   work uses the image.

## 4. Analysis

1. `python -m sarvadristhi analyze <case>/evidence.E01 --case <case> --case-id <id> --examiner "<name>"`
   or use the UI (`python -m sarvadristhi ui`).
2. Confirm in the report that the evidence hash is identical before and after analysis and
   that the custody ledger is intact (`python -m sarvadristhi verify-custody <case>`).
3. Read the detected format family and its confidence. If the family is `unknown`, the
   universal path was used: camera and time come from the video overlay or sensor
   fingerprint, and every clip says so.
4. Review findings in severity order. For each critical finding (format, index reset,
   clock change), confirm it independently: the system log entry, the frames concerned, the
   overlay readings.
5. Review the timeline. Apply the recorder-clock offset from step 1.2 when stating real time.
6. Play every clip you will rely on. Confirm camera, scene and time visually.

## 5. Machine triage

Motion, object, face and caption outputs are leads. Never put them in a statement or the
certificate without an examiner viewing the frames and describing what they see in their
own words.

## 6. Reporting and the BSA s.63(4) certificate

1. The PDF report states method, tools and versions, hashes, findings, evidence levels and
   limitations. Attach it to your own written report.
2. Annex A collects the data the s.63(4) Schedule certificate asks for (device details, hash
   algorithm and values, produced records with hashes). Transcribe it onto the official
   Schedule form: Part A is completed by the party producing the record; Part B by the
   expert. Fields marked ✎ must be completed by a person.
3. Produce exported clips with their SHA-256 values. The raw `.h264` file preserves every
   recovered frame; the `.mp4` is the playable copy.

## 7. Storage, retention and privacy

- Keep images encrypted at rest and offline. Recordings contain people unrelated to the case.
- Share only the clips that matter, with their hashes.
- Retain or destroy per the agency's evidence retention policy; log destruction in the case.

## Checklist

- [ ] Scene photographs, clock comparison photographed and offset recorded
- [ ] Device details and custody handover recorded
- [ ] Live pull (if needed) with ledger entries
- [ ] Disk imaged through a hardware write-blocker; E01 verified
- [ ] Analysis run; source hash unchanged; ledger intact
- [ ] Critical findings independently confirmed
- [ ] Relied-on clips viewed; triage items confirmed or discarded
- [ ] Report and s.63(4) data sheet reviewed and signed
