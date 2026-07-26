"""Inbox-based MP3 uploader — thin orchestrator reusing the shared pipeline.

Scans *mp3_inbox* for ``.mp3`` files, fingerprints them to determine
identity, then runs them through the same post-processing pipeline as
live-stream recordings (register + enrich + CAA/MB + popularity + dedup
+ ONE tag write).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from pathlib import Path

from radio_ripper.domain.models import (
    EnrichedInfo,
    FingerprintResult,
    MusicBrainzData,
    SavedTrack,
    TrackInfo,
)
from radio_ripper.infra.config import Settings
from radio_ripper.infra.http import HttpxAsyncClient
from radio_ripper.services.base_processor import BaseInboxProcessor
from radio_ripper.services.fingerprint import (
    FingerprintError,
    FingerprintProvider,
    NonRetriableFingerprintError,
)
from radio_ripper.services.lyrics import LRCLibProvider
from radio_ripper.services.metadata import CoverArtProvider, MetadataProvider
from radio_ripper.services.popularity import PopularityProvider, maybe_delete_obscure
from radio_ripper.services.repository import TrackRepository
from radio_ripper.services.storage import (
    compute_file_path,
    read_acoustid_score,
    remove_empty_parents,
    sanitize_filename,
)
from radio_ripper.services.tagging import TrackTagger


class Uploader(BaseInboxProcessor):
    """Scans *mp3_inbox* for ``.mp3`` files, processes each through
    fingerprint → shared pipeline (register, enrich, tag, album-move,
    CAA cover, popularity, dedup).

    Files that match a known recording are routed to ``destination/``.
    Unmatched or failed files are moved to *temp_dir*.
    Corrupt files are deleted.
    """

    def __init__(
        self,
        inbox: Path,
        temp_dir: Path,
        settings: Settings,
        fingerprint_provider: FingerprintProvider,
        metadata_provider: MetadataProvider,
        repository: TrackRepository,
        tagger: TrackTagger,
        *,
        name: str = "inbox",
        poll_interval: float = 60.0,
        cover_provider: CoverArtProvider | None = None,
        popularity_provider: PopularityProvider | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        super().__init__(inbox, temp_dir, name=name, poll_interval=poll_interval, logger=logger)
        self._settings = settings
        self._fingerprint = fingerprint_provider
        self._metadata = metadata_provider
        self._repo = repository
        self._tagger = tagger
        self._cover_provider = cover_provider
        self._popularity_provider = popularity_provider

    async def _process_file(self, proc_path: Path) -> None:
        try:
            result = await self._fingerprint.fingerprint(proc_path)
        except NonRetriableFingerprintError:
            self._log.warning("[DELETE] %s — Grund: Datei korrupt/nicht lesbar", proc_path.name)
            self._cleanup_file(proc_path)
            return
        except FingerprintError:
            self._log.warning(
                "[TEMP] %s — Grund: Fingerprint-Infrastrukturfehler, verschoben zur Prüfung",
                proc_path.name,
            )
            self._move_to_temp(proc_path)
            return

        if result is None or not result.recording_id:
            self._log.info("[TEMP] %s — Grund: kein AcoustID-Treffer, verschoben zur Prüfung", proc_path.name)
            self._move_to_temp(proc_path)
            return

        if result.score < self._settings.acoustid_min_score:
            self._log.info(
                "[DELETE] %s — Grund: Score %.2f < Mindestwert %.2f",
                proc_path.name,
                result.score,
                self._settings.acoustid_min_score,
            )
            self._cleanup_file(proc_path)
            return

        stream_title = f"{result.artist} - {result.title}"
        track = TrackInfo.from_stream_title(stream_title)

        # ── Ziel-Pfad bestimmen ──
        target_path = compute_file_path(
            self._settings.destination,
            result.artist,
            result.title,
            stream_title,
        )

        # ── Score-basierte Entscheidung ──
        if target_path.exists():
            existing_score = read_acoustid_score(target_path)
            if existing_score is not None and existing_score >= result.score:
                self._log.info(
                    "[DELETE] %s — Grund: existierende Datei hat besseren/gleichen Score (%.4f >= %.4f)",
                    proc_path.name,
                    existing_score,
                    result.score,
                )
                self._cleanup_file(proc_path)
                return
            self._log.info(
                "[DELETE] %s — Grund: neue Datei hat besseren Score (%.4f > %.4f), alte gelöscht",
                target_path,
                result.score,
                existing_score or 0.0,
            )
            target_path.unlink(missing_ok=True)
            remove_empty_parents(target_path, self._settings.destination)

        # ── Move .mp3 → destination/artist/title.untested.mp3 ──
        untested = target_path.with_name(target_path.stem + ".untested" + target_path.suffix)
        untested.parent.mkdir(parents=True, exist_ok=True)
        try:
            proc_path.rename(untested)
        except OSError:
            self._log.error("[DELETE] %s — Grund: Verschieben nach %s fehlgeschlagen", proc_path.name, untested)
            self._move_to_temp(proc_path)
            return

        # ── DB-register (nur Metadaten, kein Tag-Schreiben) ──
        provenance = f"uploader/{self._name}"
        try:
            await self._repo.register(
                SavedTrack(
                    stream_title=track.stream_title,
                    artist=track.artist,
                    title=track.title,
                    file_path=str(untested),
                    file_size=0,
                ),
                self._name,
            )
        except Exception as exc:
            self._log.warning("[%s] early db-register: %s", self._name, exc)

        # ── iTunes-Enrichment (Daten holen, kein Tag-Schreiben) ──
        enriched: EnrichedInfo | None = None
        cover_from_enrich: bytes | None = None
        if self._metadata:
            try:
                enriched = await self._metadata.fetch(result.artist, result.title)
                if enriched and enriched.artwork_url:
                    cover_from_enrich = await self._metadata.download_image(enriched.artwork_url)
                if enriched:
                    info_name = f"{enriched.artist or result.artist} - {enriched.title or result.title}"
                    self._log.info(
                        "[%s] Enriched: %s | album=%s year=%s cover=%s",
                        self._name,
                        info_name,
                        enriched.album or "-",
                        enriched.year or "-",
                        "yes" if enriched.artwork_url else "no",
                    )
                else:
                    self._log.info("[%s] no enrichment hit for %s", self._name, stream_title)
            except Exception:
                self._log.debug("[%s] enrichment failed for %s", self._name, untested.name)

        # ── Rename .untested.mp3 → .mp3 ──
        cleaned = untested.with_name(untested.stem.replace(".untested", "") + untested.suffix)
        try:
            untested.rename(cleaned)
        except OSError as exc:
            self._log.warning("[%s] rename .untested -> .mp3 failed: %s", self._name, exc)
            self._cleanup_file(untested)
            return

        # ── Album-Unterverzeichnis ──
        final_path: Path = cleaned
        if enriched and enriched.album:
            artist_dir = sanitize_filename(enriched.artist or result.artist)
            album_dir = sanitize_filename(enriched.album)
            new_dir = self._settings.destination / artist_dir / album_dir
            new_dir.mkdir(parents=True, exist_ok=True)
            new_path = new_dir / cleaned.name
            try:
                shutil.move(str(cleaned), str(new_path))
                remove_empty_parents(cleaned, self._settings.destination)
                final_path = new_path
            except OSError as exc:
                self._log.warning("[%s] album dir move failed: %s", self._name, exc)

        # ── DB-update (file_path + enrichment) ──
        try:
            fsize = cleaned.stat().st_size if cleaned.exists() else final_path.stat().st_size
        except OSError:
            fsize = 0
        try:
            await self._repo.update_file_path(self._name, track.stream_title, str(final_path))
        except Exception as exc:
            self._log.debug("[%s] db update_file_path: %s", self._name, exc)
        try:
            await self._repo.update_enrichment(
                self._name,
                track.stream_title,
                artist=enriched.artist if enriched and enriched.artist else None,
                title=enriched.title if enriched and enriched.title else None,
                album=enriched.album if enriched else None,
                year=enriched.year if enriched else None,
                file_size=fsize,
                has_cover=(enriched is not None),
                enrichment="itunes" if enriched else "",
                label=enriched.label if enriched else None,
                track_number=enriched.track_number if enriched else None,
                disc_number=enriched.disc_number if enriched else None,
            )
        except Exception as exc:
            self._log.debug("[%s] db update_enrichment: %s", self._name, exc)

        # ── AcoustID Swap-Erkennung ──
        if (
            track.artist
            and track.title
            and result.artist.lower() == track.title.lower()
            and result.title.lower() == track.artist.lower()
        ):
            self._log.warning(
                "[%s] AcoustID artist/title swapped (%s / %s) — correcting",
                self._name, result.artist, result.title,
            )
            result = FingerprintResult(
                artist=track.artist,
                title=track.title,
                score=result.score,
                recording_id=result.recording_id,
            )

        self._log.info(
            "[%s] AcoustID match (score=%.2f): %s - %s (rec=%s)",
            self._name,
            result.score, result.artist, result.title, result.recording_id,
        )

        # ── DB-update (fingerprint) ──
        try:
            await self._repo.update_fingerprint(
                self._name, track.stream_title,
                recording_id=result.recording_id,
                score=result.score,
            )
        except Exception as exc:
            self._log.debug("[%s] db update_fingerprint: %s", self._name, exc)

        # ── CAA Cover, MusicBrainz, Deezer Artist Image (parallel) ──
        cover_from_caa: bytes | None = None
        mb_data: MusicBrainzData | None = None
        artist_image: bytes | None = None
        if self._cover_provider and result.recording_id:
            tasks: list = [
                self._cover_provider.fetch_cover_by_recording_id(result.recording_id),
                self._cover_provider.fetch_recording_data(result.recording_id),
            ]
            if self._popularity_provider and result.artist:
                tasks.append(self._popularity_provider.fetch_artist_image(result.artist))
            cov_results = await asyncio.gather(*tasks, return_exceptions=True)
            cover_from_caa = cov_results[0] if not isinstance(cov_results[0], BaseException) else None
            mb_data = cov_results[1] if not isinstance(cov_results[1], BaseException) else None
            if len(cov_results) > 2:
                artist_image = cov_results[2] if not isinstance(cov_results[2], BaseException) else None

        # ── MB-Korrektur vor Popularität/Lyrics (MB-Daten sind kanonisch) ──
        from radio_ripper.services.track_processing import correct_fingerprint_result

        corrected = correct_fingerprint_result(result, mb_data)
        if corrected is not result:
            self._log.info(
                "[%s] MB corrected artist/title: %s -> %s / %s -> %s",
                self._name,
                result.artist, corrected.artist,
                result.title, corrected.title,
            )
            result = corrected
            stream_title = f"{result.artist} - {result.title}"
            track = TrackInfo.from_stream_title(stream_title)

        # ── Popularität (Deezer) ──
        if self._settings.min_popularity_rank > 0 and self._popularity_provider and (result.artist or result.title):
            deleted = await maybe_delete_obscure(
                file_path=final_path,
                station_name=self._name,
                stream_title=stream_title,
                artist=result.artist,
                title=result.title,
                min_rank=self._settings.min_popularity_rank,
                popularity_provider=self._popularity_provider,
                repository=self._repo,
                logger=self._log,
            )
            if deleted:
                return

        # ── Cross-Station AcoustID-Dedup ──
        try:
            all_existing = await self._repo.find_all_by_recording_id(result.recording_id)
        except Exception as exc:
            all_existing = []
            self._log.debug("[%s] find_all_by_recording_id: %s", self._name, exc)

        if all_existing:
            from pathlib import Path as _Path

            candidates: list[tuple[float, str, str, _Path]] = [
                (e.track.acoustid_score or 0.0, e.station_name, e.track.stream_title, _Path(e.track.file_path))
                for e in all_existing
            ]
            candidates.append((result.score, self._name, track.stream_title, final_path))
            candidates.sort(key=lambda c: c[0], reverse=True)
            _, best_station, best_stream, best_path = candidates[0]
            for score, station, st, p in candidates:
                if (score, station, st, p) == (candidates[0][0], best_station, best_stream, best_path):
                    continue
                self._log.info(
                    "[%s] AcoustID dedup: discarding inferior (score %.2f < best %.2f): %s",
                    self._name, score, candidates[0][0], p.name,
                )
                with contextlib.suppress(OSError):
                    p.unlink(missing_ok=True)
                    remove_empty_parents(p, self._settings.destination)
                try:
                    await self._repo.remove(station, st)
                except Exception as exc:
                    self._log.debug("[%s] db remove dedup: %s", self._name, exc)

        # ── Bereinigen von ungematchten Duplikaten ──
        if track.artist and track.title:
            try:
                unmatched = await self._repo.find_all_by_artist_title(track.artist, track.title)
            except Exception:
                unmatched = []
            for rec in unmatched:
                if rec.station_name == self._name and rec.track.stream_title.lower() == track.stream_title.lower():
                    continue
                if rec.track.acoustid_recording_id:
                    continue
                self._log.info(
                    "[%s] Replacing unmatched recording with matched version: %s",
                    self._name, rec.track.file_path,
                )
                old_path = _Path(rec.track.file_path)
                with contextlib.suppress(OSError):
                    old_path.unlink(missing_ok=True)
                    remove_empty_parents(old_path, self._settings.destination)
                try:
                    await self._repo.remove(rec.station_name, rec.track.stream_title)
                except Exception as exc:
                    self._log.debug("[%s] db remove unmatched for replacement: %s", self._name, exc)

        # ── Lyrics ──
        lyrics: str | None = None
        try:
            lyrics_provider = LRCLibProvider(HttpxAsyncClient(), timeout=5.0)
            lyrics = await lyrics_provider.fetch(track.artist, track.title)
        except Exception:
            self._log.debug("[%s] Lyrics fetch failed for %s", self._name, final_path.name)

        # ── EIN Tag-Schreib-Durchgang ──
        final_cover = cover_from_caa or cover_from_enrich
        try:
            self._tagger.write_all(
                final_path,
                track,
                provenance,
                enriched=enriched,
                cover_bytes=final_cover,
                recording_id=result.recording_id,
                score=result.score,
                mb_data=mb_data,
                artist_image=artist_image,
                lyrics=lyrics,
            )
            if final_cover is not None:
                self._log.info("[%s] CAA cover embedded: %s", self._name, final_path.name)
            if lyrics:
                self._log.info("[%s] Lyrics found for %s (%d chars)", self._name, final_path.name, len(lyrics))
        except Exception as exc:
            self._log.warning("[%s] Tag write failed: %s", self._name, exc)

        self._log.info("[%s] Completed: %s (%d bytes)", self._name, final_path.name, fsize)


__all__ = ["Uploader"]
