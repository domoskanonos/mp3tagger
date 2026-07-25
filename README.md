# radio-ripper-tag

**Webradio-Tagger** — AcoustID-Fingerprinting, iTunes-Enrichment, ID3v2-Tagging

[![CI](https://github.com/domoskanonos/radioripper/actions/workflows/ci.yml/badge.svg)](https://github.com/domoskanonos/radioripper/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python >=3.11](https://img.shields.io/badge/python-3.11+-blue.svg)](pyproject.toml)

---

## Quick-Start

### Docker (empfohlen)

```bash
docker compose up -d
```

Konfiguration via `config.docker.json` anpassen, `.env` mit `ACOUSTID_API_KEY` befüllen.

### Lokale Installation

```bash
# Voraussetzungen: Python >=3.11, uv, ffmpeg, libchromaprint-tools
uv sync
uv run radio-ripper --config config.json
```

---

## Konfiguration

Die Konfiguration erfolgt über eine JSON-Datei (siehe `config.json` / `config.docker.json`):

| Feld | Typ | Standard | Beschreibung |
|------|-----|----------|--------------|
| `destination` | string | `./recordings` | Zielverzeichnis für getaggte MP3s |
| `work_dir` | string | `./work` | Arbeitsverzeichnis (DB, Logs, Inbox) |
| `log_level` | string | `INFO` | Loglevel (DEBUG/INFO/WARNING/ERROR/CRITICAL) |
| `min_popularity_rank` | int | `100000` | Deezer-Popularitätsschwelle (0 = deaktiviert) |
| `acoustid_min_score` | float | `0.85` | Minimale AcoustID-Confidence (0.0–1.0) |

---

## Entwicklung

```bash
uv sync --group dev
uv run pytest --cov -q
uv run ruff check src/ tests/
uv run ruff format --check src/ tests/
uv run mypy src/radio_ripper/
pre-commit run --all-files
```

---

## Architektur

Das Projekt folgt einer **hexagonalen / Ports-and-Adapters-Architektur**:

```
CLI (cli.py)
  │
  ▼
Services Layer          ← ABCs definieren Ports
  ├── FileProcessor      Inbox-Verarbeitung
  ├── Uploader           Manueller Upload
  ├── Fingerprint        AcoustID-Fingerprinting
  ├── Metadata           iTunes/MusicBrainz-Anreicherung
  ├── Tagging            ID3v2-Tagging (mutagen)
  ├── Repository         Persistenz (SQLite)
  └── Popularity/Lyrics  Deezer / lyrics.ovh
  │
  ▼
Domain Layer            ← Wertobjekte (dataclasses)
  └── TrackInfo, EnrichedInfo, SavedTrack, …
  │
  ▼
Infrastructure Layer    ← Adapter implementieren Ports
  ├── HTTP (httpx)       AsyncHttpClient
  ├── Config (Pydantic)  Settings-Validierung
  ├── Logging            Rotierende Filehandler
  └── Resilience         retry_async-Decorator
```

**Externe Dienste:** AcoustID, iTunes Search API, MusicBrainz, Cover Art Archive, Deezer, lyrics.ovh

---

## CI/CD

- **Lint:** ruff check + ruff format
- **Type-Check:** mypy (strict mode)
- **Test:** pytest + coverage (Python 3.11–3.13)
- **Docker:** Multi-Stage-Build, Push zu Docker Hub (main/master)

---

## Lizenz

MIT — siehe [LICENSE](LICENSE).
