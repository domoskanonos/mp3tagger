# API-Audit: Jain - Lil Mama

Beispiel-MP3: `work/Jain - Lil Mama.mp3`
StreamTitle: `"Jain - Lil Mama"`

---

## 1. AcoustID (Fingerprint)

**API:** `acoustid.match()` (Chromaprint → AcoustID API)

```json
{
  "score": 0.9619264,
  "recording_id": "893b4a03-6208-4b61-b414-35a894e50369",
  "artist": "Lil Mama",
  "title": "Jain"
}
```

**⚠️ Künstler und Titel sind VERTAUSCHT.**  
AcoustID gibt die Werte in der falschen Reihenfolge zurück.

**Genutzte Felder:** `recording_id`, `score`  
**Code:** `fingerprint.py:85` → `FingerprintResult` → `processor.py:123`

---

## 2. iTunes Search API

**API:** `GET https://itunes.apple.com/search?term=Jain%20Lil%20Mama&limit=1&entity=song&media=music`

```json
{
  "artist": "Jain",
  "title": "Lil Mama",
  "album": "Zanaka (Deluxe)",
  "year": "2015",
  "genre": "Pop",
  "label": null,
  "track_number": 4,
  "disc_number": 1,
  "track_length": 158213,
  "artwork_url": "https://is1-ssl.mzstatic.com/image/thumb/Music125/v4/5a/78/86/5a788626-308e-eb19-80e3-1b3b78ef1fe8/886446194783.jpg/600x600bb.jpg",
  "itunes_data": {
    "track_id": 1175094279,
    "artist_id": 334329603,
    "collection_id": 1175093890,
    "track_count": 16,
    "disc_count": 1,
    "country": "USA",
    "explicitness": "notExplicit"
  }
}
```

**Wichtige Beobachtungen:**
- `label` (recordLabel) ist `null` → TPUB wird in `write_full` NICHT gesetzt
- Künstler/Titel sind korrekt (auch bei vertauschter Suchanfrage)
- `disc_count=1` → existiert in der API, wird aber nicht als Frame `TPOS` genutzt (nur `disc_number`)

**Code:** `metadata.py:89-106` → `EnrichedInfo` → `tagging.py:218-322` (write_full)

---

## 3. MusicBrainz Recording API

**API:** `GET https://musicbrainz.org/ws/2/recording/893b4a03-6208-4b61-b414-35a894e50369?fmt=json&inc=releases+isrcs+genres`

```json
{
  "id": "893b4a03-6208-4b61-b414-35a894e50369",
  "title": "Lil Mama",
  "length": 158000,
  "first-release-date": "2015-11-06",
  "isrcs": ["FR9W11504983"],
  "genres": [
    {"name": "afrobeat"},
    {"name": "indie pop"},
    {"name": "pop"}
  ],
  "releases": [
    {"id": "b878e72b-...", "title": "Zanaka", "date": "2015-11-06", "country": "XW", "status": "Official"},
    {"id": "7a5783f7-...", "title": "Zanaka", "date": "2015-12-04", "country": "FR", "status": "Official"}
  ]
}
```

**Genutzte Felder:** `isrcs`, `genres`, `length`, `releases[].id` (für Release-Detail)

---

## 4. MusicBrainz Release API

**API:** `GET https://musicbrainz.org/ws/2/release/b878e72b-...?fmt=json&inc=labels+release-groups`

```json
{
  "id": "b878e72b-3278-460c-91cf-cd77c85c862a",
  "title": "Zanaka",
  "date": "2015-11-06",
  "country": "XW",
  "barcode": "886445536461",
  "status": "Official",
  "label-info": [
    {
      "catalog-number": null,
      "label": {"name": "Columbia"}
    }
  ],
  "release-group": {
    "primary-type": "Album",
    "secondary-types": []
  }
}
```

**Genutzte Felder:** `label-info[0].label.name` (→ TPUB), `catalog-number`, `barcode`, `release-group.primary-type`, `release-group.secondary-types`, `date`, `country`

**Wichtige Beobachtung:**
- `catalog-number` ist `null` → TXXX:CatalogNumber wird nicht gesetzt
- `label=Columbia` → TPUB wird aus MB gesetzt (iTunes hatte keinen Label)
- Diese Daten werden erst im 3. Durchgang (`write_fingerprint_tags`) geschrieben

---

## 5. Cover Art Archive (CAA)

**API:** `GET https://coverartarchive.org/release/b878e72b-.../front`

```http
HTTP/2 307
location: https://archive.org/download/mbid-b878e72b-.../mbid-b878e72b-...-12053748431.jpg
```

Beide Releases (Worldwide + France) haben CAA-Cover.
Das CAA-Cover wird in `write_fingerprint_tags` geschrieben und **überschreibt** das iTunes-Cover aus `write_full`.

---

## 6. LRCLIB (Lyrics)

**API:** `GET https://lrclib.net/api/get?artist_name=Jain&track_name=Lil%20Mama`

```json
{
  "plainLyrics": "Hey, little mama\nWhy don't you come around?...",
  "syncedLyrics": "[00:00.75] (Yeah, yeah, yeah, yeah)...",
  "instrumental": false,
  "duration": 158.0
}
```

**Lyrics gefunden:** 1555 Zeichen Plain, synced LRC vorhanden.

---

## 7. Deezer (Artist Image & Popularity)

**API Suche:** `GET https://api.deezer.com/search/artist?q=Jain`

```
Jain (id=5951582, nb_fan=438682, nb_album=17)
  picture: https://cdn-images.dzcdn.net/images/artist/4081ad8f96b9b8215a1a42e45ae7d17b/250x250-...
```

**API Track-Suche:** `GET https://api.deezer.com/search?q=Jain%20Lil%20Mama`

Keine direkte Track-Übereinstimmung (andere Ergebnisse). Die Popularity-Prüfung würde bei diesem Track fehlschlagen, da der Track nicht im Deezer-Katalog ist.

---

## Zusammenfassung: Welche API liefert welche Felder?

| ID3-Frame | ICY/Stream | AcoustID | iTunes | MusicBrainz | CAA | LRCLIB | Deezer |
|-----------|-----------|----------|--------|-------------|-----|--------|--------|
| TPE1 | ✓ | ✓(swap!) | ✓ | — | — | — | — |
| TPE2 | ✓ | ✓(swap!) | ✓ | — | — | — | — |
| TIT2 | ✓ | ✓(swap!) | ✓ | — | — | — | — |
| TALB | — | — | ✓ | ✓ | — | — | — |
| TDRC (Jahr) | — | — | ✓ | ✓ | — | — | — |
| TCON (Genre) | — | — | ✓ | ✓ | — | — | — |
| TRCK (#) | — | — | ✓ | — | — | — | — |
| TPOS (#) | — | — | ✓ | — | — | — | — |
| TLEN | — | — | ✓ | ✓ | — | — | — |
| TPUB (Label) | — | — | ✓(selten) | ✓ | — | — | — |
| APIC:Cover | — | — | ✓ | — | ✓ | — | — |
| APIC:Performer | — | — | — | — | — | — | ✓ |
| TXXX:ITunes* | — | — | ✓ | — | — | — | — |
| TXXX:MB* | — | — | — | ✓ | — | — | — |
| USLT | — | — | — | — | — | ✓ | — |
| TXXX:AcoustID* | — | ✓ | — | — | — | — | — |
