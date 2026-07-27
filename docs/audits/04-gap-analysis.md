# Gap-Analyse: Probleme & Lösungen

## Problem 1: 4 separate Saves (Verschwendung + Fehlerquelle)

**Status:** ❌

Jeder `audio.save()`-Durchgang öffnet die MP3, liest alle Tags, modifiziert,
schreibt alles neu. Das ist:
- ~4× langsamer als nötig
- Jeder Durchgang muss `delall()` für Frames vorheriger Durchgänge aufrufen
- Bei Absturz zwischen Save #1 und #2 sind Tags inkonsistent

**Lösung:** Datenquellen parallel abfragen, dann **einen einzigen** `audio.save()`.

## Problem 2: TLEN-Verlust bei fehlendem MB-Wert

**Status:** ❌ Bug

In `write_fingerprint_tags` (tagging.py:448-449):
```python
audio.delall("TLEN")  # ← immer gelöscht
...
if mb_data.length_ms is not None:  # ← nur geschrieben wenn MB Daten hat
    audio.add(TLEN(...))
```

Wenn MusicBrainz keine Tracklänge zurückgibt (häufig bei Non-US-Releases),
wird der iTunes-TLEN (158213ms im Beispiel) zerstört.

**Lösung:** Nur delall wenn ein Ersatzwert vorhanden ist, oder MB-TLEN mit
iTunes-Fallback kombinieren.

## Problem 3: AcoustID-Swap Künstler/Titel

**Status:** ⚠️ Datenqualität (nicht Code)

AcoustID liefert für `Jain - Lil Mama`:
```json
{"artist": "Lil Mama", "title": "Jain"}  // VERTAUSCHT
```

Der Code verwendet `result.artist/title` für:
- Dateiname/-pfad (`compute_file_path`)
- `write_basic` (TPE1, TIT2) → falsch, aber write_full korrigiert aus iTunes

**Effekt:**
- Datei landet im falschen Ordner → `destination/Lil Mama/...` statt `destination/Jain/...`
- Bei fehlendem iTunes-Match bleiben die vertauschten Tags dauerhaft

**Lösung:** 
- `write_basic` überspringen oder TrackInfo aus ICY-Metadaten (nicht aus
  AcoustID) verwenden
- Oder: AcoustID-Ergebnis mit ICY-StreamTitle abgleichen

## Problem 4: Fehlende Frames in bestimmten Szenarien

### TPUB fehlt wenn weder iTunes noch MB einen Label liefern
**Status:** ✅ Absicht (kein Fallback auf Station-Name)

### disc_count von iTunes wird nicht als Frame genutzt
**Status:** ⚠️ Niemand mapped disc_count auf einen ID3-Frame
TXXX:ITunesDiscCount existiert, aber `TPOS` nutzt nur `disc_number`.

### TXXX:CatalogNumber wird nicht geschrieben wenn catalog_number=null
**Status:** ✅ Korrekt (kein Dummy-Wert)

## Problem 5: Enrichment & Fingerprint laufen asynchron aber schreiben getrennt

**Status:** ⚡ Optimierung

```python
# track_processing.py: enrich_and_file()
tagger.write_basic(...)       # Save #1
tagger.write_full(...)         # Save #2

# track_processing.py: fingerprint_song()
tagger.write_fingerprint_tags(...)  # Save #3

# processor.py / uploader.py
tagger.write_lyrics(...)            # Save #4
```

Diese 4 Aufrufe könnten zu einem zusammengefasst werden, weil alle
Datenquellen (ICY + iTunes + AcoustID + MB + CAA + Deezer + LRCLIB)
vor dem ersten `audio.save()` verfügbar sind.

## Problem 6: Doppelordner (album == artist)

**Status:** ✅ Kein Code-Bug (Datenproblem)

Wenn iTunes `album == artist` liefert, entsteht `Artist/Artist/datei.mp3`.
iTunes hat kein separates "Album Artist"-Feld für TPE2. Das ist nicht
fixbar ohne externe Datenquelle.

## Zusammenfassung der Bugs

| # | Problem | Schwere | Betroffener Code |
|---|---------|---------|------------------|
| 1 | 4 Saves statt 1 | Mittel | tagging.py + track_processing.py |
| 2 | TLEN-Verlust | **Hoch** | tagging.py:448-449 |
| 3 | AcoustID-Swap | **Hoch** | processor.py + uploader.py |
| 4 | disc_count ungenutzt | Niedrig | tagging.py (write_full) |

## Offene Fragen

1. Wird die `update_musicbrainz_metadata()`-Methode überhaupt noch aufgerufen?
   (Scheint nicht im aktuellen Pipeline-Flow vorzukommen — alles geht über
   `write_fingerprint_tags`.)
2. Sollen synced LRC-Lyrics aus LRCLIB zusätzlich als TXXX:SyncedLyrics
   geschrieben werden?
3. `disc_count` aus iTunes — soll `TPOS` als `"disc_number/disc_count"`
   formatiert werden (wie `TRCK` mit `track_number/track_count`)?
