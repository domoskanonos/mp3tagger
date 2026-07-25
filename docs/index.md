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
- Externe APIs: AcoustID, iTunes, MusicBrainz, CAA, Deezer, lyrics.ovh
- SQLite für Persistenz (single-user, kein Netzwerk)
- System-Chromaprint für Audio-Fingerprinting

## 3. Kontextabgrenzung

![Systemkontext](diagrams/context.puml)

**Fachlicher Kontext:** Der Tagger verarbeitet MP3-Dateien aus einem Inbox-Verzeichnis, reichert sie mit Metadaten an und verschiebt sie in ein Zielverzeichnis.

## 4. Lösungsstrategie

- **Hexagonale Architektur:** Domain-Modelle (dataclasses) sind frei von Infrastruktur-Code
- **ABCs als Ports:** `FingerprintProvider`, `MetadataProvider`, `TrackTagger`, `TrackRepository`
- **Asynchron:** Vollständig async/await mit asyncio
- **Pipeline:** Fingerprint → Enrich → Tag → Cover → Popularity → Dedup → Lyrics → Filing

## 5. Bausteinsicht

![Bausteinsicht](diagrams/building_blocks.puml)

### Ebene 1 – Schichten

| Schicht | Verzeichnis | Zuständigkeit |
|---------|------------|---------------|
| CLI | `cli.py` | Argument-Parsing, DI-Wiring, Signal-Handling |
| Services | `services/` | Geschäftslogik, Provider-ABCs, Pipeline |
| Domain | `domain/` | Wertobjekte (dataclasses) |
| Infrastructure | `infra/` | HTTP-Client, Config, Logging, Resilience |

### Ebene 2 – Services

| Modul | Verantwortung |
|-------|---------------|
| `base_processor.py` | Gemeinsamer Polling-Loop für FileProcessor/Uploader |
| `processor.py` | Inbox-Verarbeitung ohne DB |
| `uploader.py` | Inbox-Verarbeitung mit DB-Persistenz |
| `fingerprint.py` | AcoustID-Fingerprinting (Chromaprint) |
| `metadata.py` | iTunes/MusicBrainz/CoverArt-Metadaten |
| `tagging.py` | ID3v2-Tagging (mutagen), Cover-Skalierung |
| `track_processing.py` | Pipeline-Funktionen (enrich, fingerprint, dedup) |
| `repository.py` | SQLite-Persistenz (WAL-Mode) |
| `popularity.py` | Deezer-Popularitätscheck |
| `lyrics.py` | lyrics.ovh-Songtexte |
| `storage.py` | Datei-Pfade, Sanitize, Remux |

## 6. Laufzeitsicht

![Runtime Flow](diagrams/runtime_flow.puml)

**Hauptprozess:**
1. FileProcessor pollt Inbox alle 2 Sekunden
2. MP3 wird nach `.processing` umbenannt (Lock-Mechanismus)
3. AcoustID-Fingerprinting → bei Match: ID3-Tagging + Album-Move
4. Cover Art Archive, Deezer-Popularität, lyrics.ovh parallel
5. Cross-Station-Dedup über AcoustID-Recording-ID

## 7. Verteilungssicht

![Deployment](diagrams/deployment.puml)

**Docker-Deployment:**
- Multi-Stage-Build (Builder mit uv, Runtime mit ffmpeg + chromaprint)
- Container läuft als unprivilegierter Benutzer `ripper`
- Volumes: `config:ro`, `work`, `recordings`
- Healthcheck auf den asyncio-Event-Loop

## 8. Architektur-Entscheidungen

| Entscheidung | Begründung |
|-------------|------------|
| ABCs statt Protocols | Explizite Vererbungshierarchie, einfachere Tool-Unterstützung |
| httpx statt aiohttp | Saubere Async-API, integrierter Connection-Pool |
| Pydantic v2 für Config | Automatische Validierung, Aliase, Feld-Constraints |
| Mutagen für ID3v2 | De-facto-Standard für Python-MP3-Tagging |
| SQLite (WAL) | Single-User, keine DB-Installation nötig |
| asyncio + `asyncio.to_thread` | sqlite3 ist synchron — Wrapping in Executor |

## 9. Qualitätsanforderungen

- **Testabdeckung:** ≥75% (Statement-Coverage)
- **Linting:** ruff (strikte Regeln), isort-kompatibel
- **Typisierung:** mypy (strikt für src/, ignorierte externe imports)
- **CI:** Lint → TypeCheck → Test (3.11-3.13) → Docker-Build
- **Resilienz:** Retry-Decorator für HTTP-APIs, graceful shutdown

## 10. Technische Risiken

| Risiko | Gegenmaßnahme |
|--------|---------------|
| Ausfall AcoustID-API | NullFingerprintProvider, Dateien bleiben `.untested` |
| Rate-Limit MusicBrainz | 1 req/s manuell + retry_async |
| Korrupte MP3-Dateien | NonRetriableFingerprintError → sofortige Löschung |
| Race-Conditions Dateisystem | `.processing`-Rename + asyncio.Lock pro Datei |

## 11. Glossar

| Begriff | Bedeutung |
|---------|-----------|
| AcoustID | Audio-Fingerprint-Datenbank (chromaprint) |
| CAA | Cover Art Archive (MusicBrainz-Projekt) |
| ICY | Internet-Stream-Protokoll (SHOUTcast/Icecast) |
| MusicBrainz | Offene Musik-Enzyklopädie |
| WAL | Write-Ahead-Log (SQLite-Journal-Modus) |
