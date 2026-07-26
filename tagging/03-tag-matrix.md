# Tag-Matrix: Welcher Frame wird wo geschrieben/gelöscht?

## Legende

| Symbol | Bedeutung |
|--------|-----------|
| ✓ setzt | Schreibt Frame (ggf. überschreibend) |
| del | Löscht Frame via `delall()` |
| — | Keine Aktion |
| ✗ BUG | Frame wird gelöscht aber nicht neu gesetzt |

## Frame-übergreifende Matrix

| Frame | Beschreibung | write_basic | write_full | write_fingerprint_tags | write_lyrics |
|-------|-------------|-------------|------------|------------------------|--------------|
| **TPE1** | Artist | ✓ TrackInfo | ✓ iTunes/Enriched | — | — |
| **TPE2** | Album Artist | ✓ TrackInfo | ✓ iTunes/Enriched | — | — |
| **TIT2** | Title | ✓ TrackInfo | ✓ iTunes/Enriched | — | — |
| **TALB** | Album | ✓ songtitle (Fallback) | ✓ iTunes/Enriched | — | — |
| **TDRC** | Year | — | ✓ iTunes | — | — |
| **TCON** | Genre | — | ✓ iTunes | — | — |
| **TRCK** | Track # | — | ✓ iTunes (disc/num) | — | — |
| **TPOS** | Disc # | — | ✓ iTunes disc_number | — | — |
| **TLEN** | Länge (ms) | — | ✓ iTunes | del + ✓ MB (✗) | — |
| **TRSN** | Station | ✓ provenance | ✓ provenance | — | — |
| **TPUB** | Label | — | ✓ iTunes label | del + ✓ MB label | — |
| **COMM** | Kommentar | ✓ "Recorded via…" | ✓ "Recorded via…" | — | — |
| **TXXX:RIPPEDBY** | Provenance | ✓ | ✓ | — | — |
| **APIC:Cover** | Cover Front | — | ✓ iTunes (skaliert) | del + ✓ CAA (✔ überschreibt) | — |
| **APIC:Performer** | Künstlerbild | — | — | ✓ Deezer | — |
| **TSRC** | ISRC | — | — | del + ✓ MB isrcs[0] | — |
| **TXXX:MB Recording Id** | MB Recording ID | — | — | ✓ AcoustID | — |
| **TXXX:AcoustID Score** | Score | — | — | ✓ | — |
| **TXXX:MB Release Id** | Release ID | — | — | del + ✓ MB | — |
| **TXXX:MB Release Group Type** | RG Type | — | — | del + ✓ MB | — |
| **TXXX:MB Genres** | MB Genres | — | — | del + ✓ MB | — |
| **TXXX:MB Release Title** | Release-Titel | — | — | del + ✓ MB | — |
| **TXXX:MB Release Date** | Release-Datum | — | — | del + ✓ MB | — |
| **TXXX:MB Album Release Country** | Land | — | — | del + ✓ MB | — |
| **TXXX:CatalogNumber** | Katalog-Nr. | — | — | del + ✓ MB | — |
| **TXXX:Barcode** | Barcode | — | — | del + ✓ MB | — |
| **TXXX:ITunesTrackId** | iTunes ID | — | ✓ | — | — |
| **TXXX:ITunesArtistId** | iTunes Artist ID | — | ✓ | — | — |
| **TXXX:ITunesCollectionId** | iTunes Collection ID | — | ✓ | — | — |
| **TXXX:ITunesTrackUrl** | iTunes URL | — | ✓ | — | — |
| **TXXX:ITunesPreviewUrl** | Preview URL | — | ✓ | — | — |
| **TXXX:ITunesTrackCount** | Track Count | — | ✓ | — | — |
| **TXXX:ITunesDiscCount** | Disc Count | — | ✓ | — | — |
| **TXXX:ITunesCountry** | Land | — | ✓ | — | — |
| **TXXX:ITunesExplicitness** | Explicit | — | ✓ | — | — |
| **USLT** | Lyrics | — | — | — | ✓ LRCLIB |
| **TXXX:Lyrics** | Lyrics (Android) | — | — | — | ✓ LRCLIB |

## Konflikte

### TLEN: ✗ Datenverlust bei fehlendem MB-Wert

```python
# write_fingerprint_tags:
if mb_data is not None:
    audio.delall("TLEN")  # ← löscht IMMER, auch wenn MB keinen Wert hat
    ...
    if mb_data.length_ms is not None:
        audio.add(TLEN(...))  # ← schreibt nur wenn MB einen Wert hat
```

**Effekt:** Wenn MusicBrainz keinen `length` liefert, wird der iTunes-TLEN gelöscht.
→ Frame existiert in der finalen Datei nicht, obwohl iTunes einen Wert hatte.

### TPUB: Überschreibung MB > iTunes

```python
# write_full: TPUB aus iTunes Label
if enriched.label:
    audio.add(TPUB(..., text=enriched.label))  # iTunes label

# write_fingerprint_tags:
if mb_data.release_label:
    audio.delall("TPUB")
    audio.add(TPUB(..., text=mb_data.release_label))  # MB überschreibt
```

**Effekt:** MB-Label dominiert. Ist MB vorhanden → TPUB von MB.
Ist MB nicht vorhanden → TPUB von iTunes bleibt erhalten. **Korrekt.**

### APIC:Cover: Überschreibung CAA > iTunes

```python
# write_full:
if effective_cover:
    audio.add(APIC(..., data=iTunes_cover))  # iTunes 600x600

# write_fingerprint_tags:
if cover_bytes is not None:
    audio.delall("APIC:Cover")
    audio.add(APIC(..., data=CAA_cover))  # CAA überschreibt
```

**Effekt:** CAA-Cover dominiert. Ist CAA vorhanden → CAA-Cover.
Ist CAA nicht vorhanden → iTunes-Cover bleibt. **Korrekt.**

### TPE1/TPE2/TIT2/TALB: Überschreibung iTunes > TrackInfo

```python
# write_basic:
audio.add(TPE1(..., text=track.artist))  # Aus AcoustID TrackInfo

# write_full:
artist = enriched.artist or track.artist  # Korrektur aus iTunes
audio.add(TPE1(..., text=artist))
```

**Effekt:** Bei vorhandenem iTunes-Match werden die (potenziell vertauschten)
AcoustID-Werte korrigiert. Bei fehlendem iTunes-Match bleiben die
AcoustID-Werte (inkl. Swap) erhalten. **Das ist die Stelle, wo der
AcoustID-Swap durchschlägt.**

## Reihenfolge der Überschreibungen

```
1. write_basic    ← ICY / AcoustID (fehleranfällig)
       ↓
2. write_full     ← iTunes (korrigiert TPE1/TIT2/TALB, fügt TPUB/TLEN/APIC hinzu)
       ↓
3. write_fp_tags  ← MusicBrainz (korrigiert TPUB/TLEN/APIC:Cover nur bei MB-Treffer)
       ↓
4. write_lyrics   ← LRCLIB (fügt USLT hinzu)
```

**Idee:** Alle 4 Schritte zu einem einzigen `audio.save()` zusammenfassen.
Dann entfallen die delall()-Konflikte und die 3 überflüssigen Saves.
