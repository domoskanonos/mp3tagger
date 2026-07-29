# radio-ripper-tag

**Webradio-Tagger** — automatisierte MP3-Tagging-Pipeline mit AcoustID-Fingerprinting, iTunes/MusicBrainz-Anreicherung und ID3v2-Tagging.

Einmal eingerichtet überwacht der Container ein Eingangsverzeichnis (`mp3_inbox`), taggt eingehende MP3s automatisch und verschiebt sie ins Zielverzeichnis (`destination`).

[![CI](https://github.com/domoskanonos/radioripper/actions/workflows/ci.yml/badge.svg)](https://github.com/domoskanonos/radioripper/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python >=3.11](https://img.shields.io/badge/python-3.11+-blue.svg)](pyproject.toml)

---

## Quick-Start

### Docker (empfohlen)

```yaml
# docker-compose.yml
services:
  ripper:
    image: domoskanonos/mp3tagger:latest
    container_name: radio-ripper
    restart: unless-stopped
    environment:
      - ACOUSTID_API_KEY=dein_key_hier
    volumes:
      - ./config.json:/app/config.json:ro   # optional
      - ./recordings:/app/recordings
      - ./work:/app/work
      - ./mp3_inbox:/app/mp3_inbox
```

```bash
docker compose up -d
docker compose logs -f
```

### Lokale Installation

```bash
# Voraussetzungen: Python >=3.11, uv, ffmpeg, libchromaprint-tools
uv sync
uv run radio-ripper             # nutzt Default-Pfade oder config.json aus CWD
```

---

## Konfiguration

### config.json (optional)

Eine optionale JSON-Datei im Container unter `/app/config.json`.  
Ohne Datei → alle Defaults (siehe Tabelle).

| Feld | Typ | Standard | Beschreibung |
|------|-----|----------|--------------|
| `destination` | string | `./recordings` | Zielverzeichnis für fertig getaggte MP3s |
| `work_dir` | string | `./work` | Arbeitsverzeichnis (Logs, Catalog-DB) |
| `mp3_inbox` | string | `./mp3_inbox` | Eingangsverzeichnis — hier MP3s ablegen zum Taggen |
| `log_level` | string | `INFO` | Loglevel: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `log_file` | string | `./work/radio_ripper.log` | Logdatei-Pfad |
| `acoustid_min_score` | float | `0.85` | Minimale AcoustID-Confidence (0.0–1.0) |
| `min_popularity_rank` | int | `100000` | Deezer-Popularitätsschwelle (0 = deaktiviert) |
| `metadata_timeout` | float | `8.0` | iTunes-API-Timeout in Sekunden |
| `cover_timeout` | float | `15.0` | Cover-Download-Timeout in Sekunden |
| `enable_coverartarchive` | bool | `true` | Cover Art Archive aktivieren |
| `max_concurrent` | int | `3` | Maximale parallele Tagging-Jobs (1–20) |
| `catalog_db` | string | `./work/catalog.db` | Pfad zur SQLite-Katalogdatenbank |
| `reconcile_on_startup` | bool | `true` | Katalog-⇄-Dateisystem-Abgleich beim Start |
| `max_collection_size` | int | `0` | Max. Anzahl Songs (0 = deaktiviert) |
| `enable_eviction` | bool | `false` | Eviction unwichtiger Songs bei Sammlungslimit |
| `exclude_release_group_types` | list | `["Live", "Bootleg"]` | Auszuschließende MusicBrainz-Release-Group-Types |
| `exclude_title_patterns` | list | `[]` | Regex-Muster für auszuschließende Titel |

Beispiel `config.json`:

```json
{
  "destination": "./recordings",
  "acoustid_min_score": 0.90,
  "min_popularity_rank": 100000,
  "catalog_db": "./work/catalog.db",
  "max_concurrent": 5,
  "reconcile_on_startup": true,
  "max_collection_size": 10000,
  "enable_eviction": true,
  "exclude_release_group_types": ["Live", "Bootleg"],
  "exclude_title_patterns": []
}
```

### Umgebungsvariablen

| Variable | Pflicht | Beschreibung |
|----------|---------|--------------|
| `ACOUSTID_API_KEY` | **Ja** | AcoustID API-Key — [kostenlos beantragen](https://acoustid.org/api-key) |

**Konfig-Hierarchie** (niedrigste → höchste Priorität):

1. Code-Defaults
2. `config.json` aus `/app/config.json`
3. Umgebungsvariablen (`ACOUSTID_API_KEY`)

---

## Volumes

| Volume (Host → Container) | Zweck |
|---------------------------|-------|
| `./mp3_inbox:/app/mp3_inbox` | **Eingang** — hier MP3s hineinlegen oder per Recording-Tool ablegen |
| `./recordings:/app/recordings` | **Ziel** — fertig getaggte MP3s landen hier, sortiert nach Interpret/Album |
| `./work:/app/work` | **Arbeit** — Logdatei + Catalog-DB |
| `./config.json:/app/config.json` | **Konfiguration** (optional, readonly) |

---

## Pipeline (8 Phasen)

Jede eingehende MP3 durchläuft:

| Phase | Beschreibung |
|-------|-------------|
| **Polling** | Überwachung von `mp3_inbox` auf neue `.mp3`-Dateien |
| **Staging** | Kopieren ins Arbeitsverzeichnis (safe rename) |
| **Fingerprinting** | Chromaprint/AcoustID — berechnet Audio-Fingerprint |
| **Metadata** | iTunes Search API — Titel, Interpret, Album, Genre |
| **Cover** | Cover Art Archive / iTunes — Album-Art als JPEG |
| **Lyrics** | LRCLib — Songtext-Suche |
| **Popularity** | Deezer — Popularitäts-Ranking |
| **Tagging** | ID3v2-Schreiben via mutagen + Verschieben ins Ziel |

---

## Architektur

```
CLI (cli.py)                    Argument-Parser, DI-Wiring, Signal-Handling
  │
  ▼
Services Layer                  ABCs definieren Ports (Provider austauschbar)
  ├── processor.py              FileProcessor: Inbox-Polling + 8-Phasen-Pipeline
  ├── fingerprint.py           AcoustID-Fingerprinting (Chromaprint)
  ├── metadata_itunes.py       iTunes MetadataProvider
  ├── metadata_musicbrainz.py  Cover Art Archive + MusicBrainz
  ├── lyrics.py                LRCLib Songtexte
  ├── popularity.py            Deezer-Popularitätscheck
  ├── tagging.py              ID3v2-Tagging (mutagen)
  └── file_utils.py           sanitize_filename, compute_file_path, safe_unlink
  │
  ▼
Domain Layer                    frozen dataclasses
  └── TrackInfo, EnrichedInfo, MusicBrainzData, FingerprintResult, ITunesTrackData
  │
  ▼
Infrastructure Layer            Adapter implementieren Ports
  ├── http.py                  AsyncHttpClient ABC + HttpxAsyncClient
  ├── config.py                Pydantic Settings-Validierung
  ├── logging.py               Rotierende Filehandler
  ├── resilience.py            retry_async (exponential backoff)
  └── errors.py                RadioRipperError-Hierarchie
```

**SQLite-Katalog-Index** — Dateisystem bleibt Source-of-Truth, Catalog ist durchsuchbarer
Index für Duplikatserkennung und Collection-Management.

**Externe Dienste:** AcoustID, iTunes Search API, MusicBrainz, Cover Art Archive, Deezer, LRCLib

---

## Collection Management

- **Duplikatserkennung** über ISRC — bei Konflikt gewinnt höherer AcoustID-Score / Bitrate
- **Eviction** bei Sammlungslimit — der Song mit niedrigstem Deezer-Rank wird gelöscht
- **Ausschluss** von Live-/Bootleg-Aufnahmen via `exclude_release_group_types`
- **Reconcile** beim Start — Katalog-Einträge vs. tatsächliche Dateien

---

## Tooling-Stack

| Tool | Verwendung |
|------|------------|
| UV | Paketmanager |
| Pydantic v2 | Konfigurations-Validierung |
| Pytest | Testing (243 Tests) |
| MyPy | Typsicherheit |
| Ruff | Code-Formatierung + Linting |
| GitHub Actions | CI/CD + Semantic-Release |
| Docker | Containerisierung, Multi-Stage-Build |

---

## Entwicklung

```bash
uv sync --group dev
uv run pytest --cov -q
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/radio_ripper/ tests/
```

### Teststruktur (1:1 mit Source)

| Source | Test |
|--------|------|
| `cli.py` | `tests/test_cli.py` |
| `domain/models.py` | `tests/domain/test_models.py` |
| `infra/` | `tests/infra/` (config, errors, http, logging, resilience) |
| `services/processor.py` | `tests/services/test_processor.py` |
| `services/file_utils.py` | `tests/services/test_file_utils.py` |
| `services/fingerprint.py` | `tests/services/test_fingerprint.py` |
| `services/metadata_itunes.py` | `tests/services/test_metadata_itunes.py` |
| `services/metadata_musicbrainz.py` | `tests/services/test_metadata_musicbrainz.py` |
| `services/lyrics.py` | `tests/services/test_lyrics.py` |
| `services/popularity.py` | `tests/services/test_popularity.py` |
| `services/tagging.py` | `tests/services/test_tagging.py` |
| `services/collection_manager.py` | `tests/services/test_collection_manager.py` |
| `services/processor.py` (Catalog) | `tests/services/test_processor_catalog.py` |
| `infra/catalog.py` | `tests/infra/test_catalog.py` |

---

## CI/CD

- **Lint:** ruff check + ruff format
- **Type-Check:** mypy auf `src/` + `tests/`
- **Test:** pytest + coverage (Python 3.11–3.13)
- **Release:** Semantic-Release (auto-bump via Conventional Commits)
- **Docker:** Multi-Stage-Build, Push zu Docker Hub mit Version-Tags

### Image-Tags

| Tag | Beschreibung |
|-----|--------------|
| `:latest` | Neuester Stand des `main`-Branches |
| `:{version}` | Semantische Versions-Tags (z. B. `2.3.0`) |

---

## Changelog & Lizenz

- **GitHub:** [domoskanonos/mp3tagger](https://github.com/domoskanonos/mp3tagger)
- **Changelog:** [CHANGELOG.md](CHANGELOG.md)
- **Lizenz:** MIT — siehe [LICENSE](LICENSE)
