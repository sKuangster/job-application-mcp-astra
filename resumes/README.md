# Resumes

Put your resume files here (PDF recommended; DOCX accepted). They are git-ignored.

Selection is deterministic:
1. The job is classified (title keywords, then Simplify category) using
   `preferences.yaml → resumes.categories` (quant, ml, data, hardware, product, swe by default).
2. The category maps to a file via `resumes.mappings` (explicit), otherwise via filename
   tokens in `resumes.filename_aliases` — e.g. `Jane_Doe_ML.pdf` → ml, `resume_swe.pdf` → swe.
3. Otherwise `resumes.default` (or the only file present).

The chosen file and the reason are recorded on every application ("Resume" in the dashboard).
The uploaded filename is what employers see, so name files professionally, e.g.
`Jane_Doe_Resume_SWE.pdf`, `Jane_Doe_Resume_ML.pdf`.
Other documents (e.g. a transcript) are uploaded only when a form requires them and a file whose
name contains the document type (e.g. `Jane_Doe_transcript.pdf`) exists here.
