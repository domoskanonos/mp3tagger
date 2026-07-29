# radio-ripper-tag – Arc42-Dokumentation

## Projektübersicht

| Attribut | Wert |
|----------|------|
| Name | radio-ripper-tag |
| Version | 2.1.0 |
| Beschreibung | Webradio-Tagger — AcoustID-Fingerprinting, iTunes-Enrichment, ID3v2-Tagging |
| Sprache | Python ≥3.11 |
| Lizenz | MIT |

## 1. Einführung und Ziele

**Hauptziel:** Automatische Identifikation und Metadaten-Anreicherung von Webradio-Mitschnitten.

**Qualitätsziele:**
- Korrekte ID3v2-Tagging auch bei unvollständigen Metadaten
- Robuster Betrieb (Resilienz gegen API-Fehlschläge, Netzwerkausfälle)
- Austauschbare externe Provider (Ports-and-Adapters)

## 2. Randbedingungen

- Linux als Zielplattform (Docker-Container)
- Externe APIs: AcoustID, iTunes, MusicBrainz, CAA, Deezer, LRCLib
- **SQLite-Katalog** — Dateisystem ist Source-of-Truth, Catalog ist durchsuchbarer Index for Duplikatserkennung und Collection-Management
- System-Chromaprint für Audio-Fingerprinting

## 3. Kontextabgrenzung

![Systemkontext](diagrams/context.puml)

**Fachlicher Kontext:** Der Tagger verarbeitet MP3-Dateien aus einem Inbox-Verzeichnis, reichert sie mit Metadaten an und verschiebt sie in ein Zielverzeichnis.

## 4. Lösungsstrategie

- **Hexagonale Architektur:** Domain-Modelle (dataclasses) sind frei von Infrastruktur-Code
- **ABCs als Ports:** `FingerprintProvider`, `MetadataProvider`, `CoverArtProvider`, `PopularityProvider`, `LyricsProvider`, `TrackTagger`, `AsyncHttpClient`
- **Asynchron:** Vollständig async/await mit asyncio, `asyncio.gather` für parallele API-Calls
- **Ein-Tag-Write-Prinzip:** Alle ID3-Frames in einem einzigen `write_all`-Durchgang (atomic via `ID3.save`)
- **SQLite-Katalog:** ISRC-basierte Duplikatserkennung, Eviction, Live/Bootleg-Ausschluss, Reconcile beim Start
- **Pipeline:** Fingerprint → CAA+MB parallel → MB-Korrektur → iTunes+Lyrics+ArtistImage parallel → Score-Dedup (Catalog + Legacy-Fallback) → Popularität → Catalog-Upsert → Eviction → Tag → atomic Move

## 5. Bausteinsicht

![Bausteinsicht](diagrams/building_blocks.puml)

### Ebene 1 – Schichten

| Schicht | Verzeichnis | Zuständigkeit |
|---------|------------|---------------|
| CLI | `cli.py` | Argument-Parsing, DI-Wiring, Signal-Handling, `_run_pipeline` |
| Services | `services/` | Geschäftslogik, Provider-ABCs, Pipeline |
| Domain | `domain/` | Wertobjekte (frozen dataclasses) |
| Infrastructure | `infra/` | HTTP-Client, Config, Logging, Resilience |

### Ebene 2 – Module

| Modul | Verantwortung |
|-------|---------------|
| `cli.py` | Arg-Parser, DI-Wiring, Signal-Handling, `_run_pipeline` |
| `services/processor.py` | FileProcessor: Inbox-Polling + 8-Phasen-Pipeline |
| `services/fingerprint.py` | AcoustID via pyacoustid (Chromaprint) |
| `services/metadata_itunes.py` | iTunes Search API-Provider |
| `services/metadata_musicbrainz.py` | MusicBrainz + Cover Art Archive |
| `services/lyrics.py` | LRCLib-API-Provider |
| `services/popularity.py` | Deezer-Ranking + `maybe_delete_unpopular` |
| `services/tagging.py` | ID3v2-Tag-Writer (mutagen), `read_acoustid_score`, `_scale_cover` |
| `services/collection_manager.py` | `is_same_version`, `is_better_version`, `should_exclude_as_live`, `pick_eviction_candidate` |
| `services/file_utils.py` | `sanitize_filename`, `compute_file_path`, `safe_unlink` |
| `domain/models.py` | `TrackInfo`, `EnrichedInfo`, `MusicBrainzData`, `FingerprintResult`, `ITunesTrackData` |
| `infra/catalog.py` | `Catalog` ABC, `SqliteCatalog`, `read_tags_from_file`, `read_audio_from_file`, `ReconcileReport`, `SongRecord` |
| `infra/http.py` | `AsyncHttpClient` ABC + `HttpxAsyncClient` + `download_image_or_none` |
| `infra/config.py` | Pydantic `Settings` + `load_settings` |
| `infra/errors.py` | `RadioRipperError` → `ConfigurationError` / `TaggingError` |
| `infra/logging.py` | `configure_logging` (Console + RotatingFile) |
| `infra/resilience.py` | `retry_async` Decorator (exponential backoff) |

## 6. Laufzeitsicht

![Runtime Flow](diagrams/runtime_flow.puml)

**Hauptprozess — FileProcessor._process_file (8 Phasen):**
1. `source/*.mp3` → `.processing`-Rename (Lock-Mechanismus)
2. Verschieben ins `work_dir`
3. AcoustID-Fingerprinting → bei `NonRetriableFingerprintError`: löschen; bei `FingerprintError`: nach `failed/` verschieben; bei Score < min: löschen
4. **Phase 1 — CAA + MB parallel:** `asyncio.gather(fetch_cover, fetch_recording_data, fetch_artist_image)` — MB ist kanonisch
5. **Phase 2 — MB-Korrektur:** `correct_fingerprint_result` überschreibt AcoustID-Artist/Title mit MB-Daten
6. **Phase 3 — iTunes + Lyrics + ArtistImage parallel:** `asyncio.gather` über alle drei Provider
7. **Phase 4 — Score-Dedup:** Vergleicht Score mit bestehender Datei in `destination/`
8. **Phase 5-8:** `.untested`-Suffix entfernen → Popularitäts-Prüfung → einmaliger `tagger.write_all` → atomarer Move zu `destination/`

## 7. Verteilungssicht

**Docker-Deployment (Multi-Stage-Build):**
- Builder-Stage mit `uv` (Python 3.12-slim) installiert Dependencies
- Runtime-Stage mit `python:3.12-slim` + ffmpeg + chromaprint
- Container läuft als root; zur Laufzeit über `--user $(id -u):$(id -g)` auf Host-User runterstufbar
- Volumes: `config`, `work`, `destination`, `source`
- Healthcheck via `pgrep -f 'radio-ripper'`
- Graceful Shutdown via SIGTERM (`stop_grace_period: 30s`)

## 8. Architekturentscheidungen

| Entscheidung | Begründung |
|-------------|------------|
| ABCs statt Protocols | Explizite Vererbungshierarchie, einfachere Tool-Unterstützung |
| httpx statt aiohttp | Saubere Async-API, integrierter Connection-Pool |
| Pydantic v2 für Config | Automatische Validierung, Aliase, Feld-Constraints |
| Mutagen für ID3v2 | De-facto-Standard für Python-MP3-Tagging |
| **SQLite-Katalog-Index** — Dateisystem = Source-of-Truth | ISRC-basierte Duplikatserkennung, Eviction, Live/Bootleg-Ausschluss; Katalog ist nur durchsuchbarer Index |
| **LRCLib statt lyrics.ovh** | Kein API-Key nötig, `plainLyrics` + `instrumental`-Flag, Free-Fallback `/api/search` |
| **Ein Tag-Schreib-Durchgang** | Einzige öffentliche Methode `write_all`, atomic via `ID3.save` |
| asyncio + `asyncio.gather` | Parallele API-Calls (iTunes + CAA + Deezer + LRCLib) |

## 9. Qualitätsanforderungen

- **Testabdeckung:** ≥80% (Statement-Coverage); ~200 Tests über `pytest --cov`
- **Linting:** ruff (E, F, W, I, N, UP, B, SIM, ARG, RUF) — ersetzt flake8 + isort
- **Typisierung:** mypy (relevante Regeln) auf `src/` + `tests/`; Pylance/`pyright` für IDE
- **CI:** Lint → TypeCheck → Test (Python 3.11-3.13) → Docker-Build (main/master)
- **Pre-Commit:** ruff + ruff-format + mypy + Standard-Hooks
- **Resilienz:** `retry_async`-Decorator für HTTP-APIs, graceful shutdown via `asyncio.Event`

## 10. Technische Risiken

| Risiko | Gegenmaßnahme |
|--------|---------------|
| Ausfall AcoustID-API | `FingerprintError` → Datei nach `failed/` verschoben (Retry bei Neustart) |
| Rate-Limit MusicBrainz | 1 req/s manuell via `_rate_limited_json` + `retry_async` |
| Korrupte MP3-Dateien | `NonRetriableFingerprintError` → sofortige Löschung |
| Race-Conditions Dateisystem | `.processing`-Rename als Lock; sequentielle Verarbeitung pro Inbox |
| Artist/Title vertauscht (AcoustID) | `correct_fingerprint_result` überschreibt mit MusicBrainz (kanonisch) |

## 11. Glossar

| Begriff | Bedeutung |
|---------|-----------|
| AcoustID | Audio-Fingerprint-Datenbank (Chromaprint) |
| CAA | Cover Art Archive (MusicBrainz-Projekt) |
| MBID | MusicBrainz Identifier (UUID für Recording/Release) |
| ICY | Internet-Stream-Protokoll (SHOUTcast/Icecast) |
| MusicBrainz | Offene Musik-Enzyklopädie |
| LRCLib | Freie Songtext-API (lrclib.net) |