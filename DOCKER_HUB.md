# radio-ripper-tag

**Webradio-Tagger** — automatisierte MP3-Tagging-Pipeline mit AcoustID-Fingerprinting, iTunes/MusicBrainz-Anreicherung und ID3v2-Tagging.

Einmal eingerichtet überwacht der Container ein Eingangsverzeichnis (`mp3_inbox`), taggt eingehende MP3s automatisch und verschiebt sie ins Zielverzeichnis (`destination`).

---

## Quick-Start

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

Ohne eigene `config.json` werden alle **Defaults** verwendet (siehe Tabelle unten).

```bash
# Starten
docker compose up -d

# Logs prüfen
docker compose logs -f
```

---

## Konfiguration

### config.json (optional)

Die Konfiguration erfolgt über eine optionale JSON-Datei.  
Standard-Pfad: `/app/config.json` im Container. Ohne Datei → alle Defaults.

```bash
# Eigene config.json mounten
volumes:
  - ./meine_config.json:/app/config.json:ro
```

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

1. `config.json` Default-Werte
2. `config.json` aus `/app/config.json`
3. Umgebungsvariablen (nur `ACOUSTID_API_KEY`)

---

## Volumes

| Volume (Host → Container) | Zweck |
|---------------------------|-------|
| `./mp3_inbox:/app/mp3_inbox` | **Eingang** — hier MP3s hineinlegen oder per Recording-Tool ablegen |
| `./recordings:/app/recordings` | **Ziel** — fertig getaggte MP3s landen hier, sortiert nach Interpret/Album |
| `./work:/app/work` | **Arbeit** — Logdatei (`radio_ripper.log`) + Catalog-DB (`catalog.db`) |
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

## Architecture (Überblick)

```
CLI (cli.py) → Services Layer → Domain Layer (TrackInfo etc.) → Infrastructure Layer
                                    │
                            SQLite Catalog (catalog.db)
                            Duplikatserkennung via ISRC
                            Eviction + Collection-Management
```

**Externe Dienste:** AcoustID, iTunes Search API, MusicBrainz, Cover Art Archive, Deezer, LRCLib

---

## Collection Management

- **Duplikatserkennung** über ISRC — bei Konflikt gewinnt höherer AcoustID-Score / Bitrate
- **Eviction** bei Sammlungslimit — der Song mit niedrigstem Deezer-Rank wird gelöscht
- **Ausschluss** von Live-/Bootleg-Aufnahmen via `exclude_release_group_types`
- **Reconcile** beim Start — Katalog-Einträge vs. tatsächliche Dateien

---

## Changelog & Source

- **GitHub:** [domoskanonos/mp3tagger](https://github.com/domoskanonos/mp3tagger)
- **Changelog:** [CHANGELOG.md](https://github.com/domoskanonos/mp3tagger/blob/main/CHANGELOG.md)
- **Lizenz:** MIT
