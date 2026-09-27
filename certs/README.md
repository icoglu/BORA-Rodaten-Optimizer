# Firmenzertifikate (optional)

Scheitert der Build mit `CERTIFICATE_VERIFY_FAILED` (Proxy mit TLS-Inspection),
hier das Root-/Proxy-Zertifikat eurer Firma als **PEM-Datei mit Endung `.crt`**
ablegen und neu bauen:

    docker compose build && docker compose up -d

Zertifikat exportieren (Windows): certmgr.msc → Vertrauenswürdige
Stammzertifizierungsstellen → Zertifikat → Exportieren → „Base-64 codiert X.509 (.CER)“,
Datei danach in `firma.crt` umbenennen.
