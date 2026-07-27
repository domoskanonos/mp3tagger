# Tagging-Pipeline: Übersicht

## 4 separate Schreibdurchgänge

```
ICYDaten (StreamTitle)
   │ "Jain - Lil Mama"
   │
   ├─ Schritt 1: write_basic() ─────────────────── SAVE #1
   │   TPE1, TPE2, TIT2, TALB, TRSN, COMM, TXXX:RIPPEDBY
   │   Quelle: TrackInfo (aus AcoustID-Ergebnis)
   │
   ├─ Schritt 2: write_full() ──────────────────── SAVE #2
   │   TPE1, TPE2, TIT2, TALB, TDRC, TCON,
   │   TRCK, TPOS, TLEN, TRSN, TPUB, COMM,
   │   TXXX:RIPPEDBY, TXXX:ITunes*, APIC:Cover
   │   Quelle: EnrichedInfo (iTunes Search API)
   │
   ├─ Schritt 3: write_fingerprint_tags() ──────── SAVE #3
   │   TXXX:MB Recording Id, TXXX:AcoustID Score,
   │   TPUB (Überschreibung), TSRC, TLEN (Überschreibung),
   │   TXXX:MB Release Id, TXXX:MB Release Group Type,
   │   TXXX:MB Genres, TXXX:MB Release Title,
   │   TXXX:MB Release Date, TXXX:MB Album Release Country,
   │   TXXX:CatalogNumber, TXXX:Barcode,
   │   APIC:Cover (CAA-Überschreibung), APIC:Performer
   │   Quelle: AcoustID + MusicBrainz + CAA + Deezer
   │
   └─ Schritt 4: write_lyrics() ────────────────── SAVE #4
       USLT, TXXX:Lyrics
       Quelle: LRCLIB
```

## Problem: Jeder Durchgang öffnet/speichert die Datei neu

Jeder `audio.save()`-Aufruf schreibt die komplette ID3-Tag-Struktur neu.
Spätere Durchgänge müssen `delall()` aufrufen, um Werte früherer
Durchgänge zu überschreiben → fehleranfällig.

### Konkrete Probleme

| Frame | write_basic | write_full | write_fingerprint_tags | write_lyrics |
|-------|-------------|------------|------------------------|--------------|
| TPE1 | setzt aus TrackInfo | **überschreibt** aus iTunes | — | — |
| TIT2 | setzt aus TrackInfo | **überschreibt** aus iTunes | — | — |
| TALB | setzt songtitle | **überschreibt** aus iTunes | — | — |
| TPUB | — | setzt aus iTunes Label | überschreibt aus MB Label | — |
| TLEN | — | setzt aus iTunes | **delall + nur schreiben wenn MB length ≠ None** | — |
| APIC:Cover | — | setzt aus iTunes | löscht+überschreibt aus CAA | — |

### Die 4 Saves sind unnötig

Alle Datenquellen (ICYDaten + iTunes + AcoustID + MB + CAA + Deezer + LRCLIB)
sind asynchron verfügbar, bevor der erste `audio.save()` nötig ist.
Ein einziger `audio.save()` am Ende würde 3 von 4 Durchgängen eliminieren.

### Datei-Pfad-Bug durch AcoustID-Swap

```
StreamTitle: "Jain - Lil Mama"
        ↓ AcoustID fingerprint
result.artist = "Lil Mama" (SWAPPED!)
result.title  = "Jain"     (SWAPPED!)
        ↓
stream_title = "Lil Mama - Jain"
track.artist = "Lil Mama"
track.title  = "Jain"
        ↓
compute_file_path(…, result.artist, result.title, …)
→ destination/Lil Mama/Lil Mama - Jain.mp3  (FALSCH)
```

→ Die AcoustID-Antwort kann Künstler/Titel vertauscht zurückliefern.
→ Der finale Dateipfad landet im falschen Ordner.
→ `write_full` korrigiert die Tags (aus iTunes), aber das ist zu spät für den Dateinamen.
