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
| `work_dir` | string | `./work` | Arbeitsverzeichnis (Logs, Inbox) |
| `mp3_inbox` | string | `./mp3_inbox` | Eingangsverzeichnis für zu taggende MP3s |
| `log_level` | string | `INFO` | Loglevel (DEBUG/INFO/WARNING/ERROR/CRITICAL) |
| `min_popularity_rank` | int | `100000` | Deezer-Popularitätsschwelle (0 = deaktiviert) |
| `acoustid_min_score` | float | `0.85` | Minimale AcoustID-Confidence (0.0–1.0) |
| `enable_coverartarchive` | bool | `true` | Cover Art Archive aktivieren |
| `metadata_timeout` | float | `8.0` | iTunes-API-Timeout (Sekunden) |
| `cover_timeout` | float | `15.0` | Cover-Download-Timeout (Sekunden) |
| `catalog_db` | string | `./work/catalog.db` | Pfad zur SQLite-Katalogdatenbank |
| `reconcile_on_startup` | bool | `true` | Katalog-⇄-Dateisystem-Abgleich beim Start |
| `max_collection_size` | int | `0` | Max. Anzahl Songs in Sammlung (0 = deaktiviert) |
| `enable_eviction` | bool | `false` | Eviction unwichtiger Songs bei Sammlungslimit |
| `exclude_release_group_types` | list[string] | `["Live", "Bootleg"]` | Auszuschließende Release-Group-Types |
| `exclude_title_patterns` | list[string] | `[]` | Regex-Muster für auszuschließende Titel |

### Umgebungsvariablen

| Variable | Beschreibung |
|----------|--------------|
| `ACOUSTID_API_KEY` | AcoustID API-Key (erforderlich) — [Hier beantragen](https://acoustid.org/api-key) |
| `ACCOUST_ID` | Legacy-Alias (veraltet — `ACOUSTID_API_KEY` bevorzugen) |

Siehe `.env.example`.

---

## Architektur

Das Projekt folgt einer **hexagonalen / Ports-and-Adapters-Architektur**:

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
  ├── tagging.py              ID3v2-Tagging (mutagen), einzige write_all-Methode
  └── file_utils.py           sanitize_filename, compute_file_path, safe_unlink
  │
  ▼
Domain Layer                    frozen dataclasses, frei von Infrastruktur
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

**SQLite-Katalog-Index** — Dateisystem bleibt Source-of-Truth, Catalog ist durchsuchbarer Index für Duplikatserkennung und Collection-Management.

**Externe Dienste:** AcoustID, iTunes Search API, MusicBrainz, Cover Art Archive, Deezer, LRCLib

---

## Collection Management & Optimization

Der **SQLite-Katalog** (`catalog.db`) trackt jeden importierten Song mit ISRC, MBID, AcoustID-Score, Bitrate, Sample-Rate und Deezer-Rank.

### Duplikatserkennung (ISRC-basiert)

Zwei Songs gelten als **gleiche Version** wenn sie denselben ISRC teilen. Bei Konflikt gewinnt: höherer AcoustID-Score → höhere Bitrate → höhere Sample-Rate.

### Eviction

Bei `enable_eviction: true` und `max_collection_size > 0` wird beim Import der Song mit dem niedrigsten Deezer-Rank gelöscht (`safe_unlink` + Katalog-Eintrag entfernt).

### Live-/Bootleg-Ausschluss

Songs deren Release-Group-Type in `exclude_release_group_types` (Default: `["Live", "Bootleg"]`) oder deren Titel auf `exclude_title_patterns` matched, werden sofort gelöscht.

### Reconcile (Katalog ⇄ Dateisystem)

Beim Start gleicht `reconcile_on_startup` die Katalog-Einträge mit dem Dateisystem ab: fehlende Dateien werden aus dem Katalog entfernt, nicht-katalogisierte Einträge werden hinzugefügt.

---

## Tooling-Stack

| Tool | Verwendung | Bemerkung |
|------|------------|-----------|
| UV | Paketmanager | `uv sync`, `uv run` |
| Pydantic v2 | Konfigurations-Validierung | `Settings`-Dataclass mit Constraints |
| Pytest | Testing | async mode=auto, 175 Tests |
| MyPy | Typsicherheit | relevante Regeln auf `src/` + `tests/` |
| Pyright/Pylance | IDE Typisierung | `reportPrivateImportUsage` für mutagen |
| Ruff | Code-Formatierung + Linting | ersetzt flake8 + isort (Regeln: E, F, W, I, N, UP, B, SIM, ARG, RUF) |
| Pre-Commit | Qualitätssicherung | ruff + mypy + Standard-Hooks |
| GitHub Actions | CI/CD | Lint → TypeCheck → Test (3.11-3.13) → Docker |
| Docker | Containerisierung | Multi-Stage-Build, Push zu Docker Hub |

---

## Entwicklung

```bash
uv sync --group dev
uv run pytest --cov -q          # 175 Tests + Coverage
uv run ruff check src/ tests/    # Linting
uv run ruff format --check src/ tests/  # Formatierung
uv run mypy src/radio_ripper/ tests/    # Typcheck
pre-commit run --all-files      # Alle Hooks
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

## Dokumentation

- [Arc42-Systemdokumentation](docs/index.md) mit PlantUML-Diagrammen
- [API-Audits und Pipeline-Analysen](docs/audits/) (Historie)
- [CHANGELOG](CHANGELOG.md)

---

## CI/CD

- **Lint:** ruff check + ruff format
- **Type-Check:** mypy auf `src/` + `tests/`
- **Test:** pytest + coverage (Python 3.11–3.13)
- **Docker:** Multi-Stage-Build, Push zu Docker Hub (main/master)

---

## Lizenz

MIT — siehe [LICENSE](LICENSE).