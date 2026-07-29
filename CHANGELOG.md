# Changelog – radio-ripper-tag

## [Unreleased] - Refactoring

### Removed
- `tag_file.py` (broken imports since metadata-split — cli.py:main is the single entrypoint)
- `tagging/` ad-hoc docs (migrated to `docs/audits/`)
- `config.docker.json`, `config.example.json` — nur noch `config.json`

### Added
- Config-Auto-Discovery: `--config` → `./config.json` → `~/.config/radio-ripper/config.json` → `/app/config/config.json`
- Ohne Config-Datei werden alle Defaults verwendet (kein Fehler mehr)

### Changed
- `Settings`-Defaults vereinfacht: `source` (vormals `mp3_inbox`), `log_file` haben feste Defaults (kein `model_validator`)
- Dockerfile: ohne `config.docker.json`, legt `/app/source` an, kein `--config` im `CMD`
- `docker-compose.yml`: Config-Volume entfernt, `source`-Volume hinzugefügt
- README aktualisiert (kein `config.docker.json`-Verweis mehr)

### Added
- `tests/services/test_processor.py` (39 tests — full FileProcessor pipeline coverage)
- `tests/domain/test_models.py` (16 tests for TrackInfo/FingerprintResult/EnrichedInfo/etc.)
- `infra/http.py:download_image_or_none` — central image-download helper (eliminates duplication)

### Changed
- `metadata_itunes.py` + `metadata_musicbrainz.py` now import `download_image_or_none` from `infra/http.py` (was duplicated `_fetch_image`)
- `test_storage.py` → `test_file_utils.py` (matches source module rename)
- `test_metadata.py` split into `test_metadata_itunes.py` + `test_metadata_musicbrainz.py` (1:1 with source)
- CI now runs `mypy` on `src/` + `tests/`
- Pre-commit mypy hook extended to `tests/`
- `.env.example` corrected — `ACOUSTID_API_KEY` is primary (was only deprecated `ACCOUST_ID`)
- `config.json` uses relative demo paths (was absolute `/home/laptop/...`)
- arc42 docs/index.md + all 3 PlantUML diagrams rewritten without dead modules
- README.md updated — no more Repository/SQLite/lyrics.ovh/SavedTrack/Uploader references

### Fixed
- Test count: 121 → 175 (54 new tests, all green)

## [2.1.0] - 2026-07-25

### Added
- Split from monorepo: eigenständiges Projekt mit eigener pyproject.toml, Dockerfile und CI

### Changed
- Tagging ohne Stream-Recording/GUI-Abhängigkeiten
- Dependencies: httpx, pydantic, mutagen, Pillow, pydub, pyacoustid
