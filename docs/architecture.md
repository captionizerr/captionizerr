# How Captionizerr works

This page covers the parts, a job's life from import to subtitle, and where data and secrets live. For setup, see the [setup guide](setup-guide.md).

## The big picture

```mermaid
flowchart TD
    subgraph Arr["Your *arr stack"]
        S[Sonarr]
        R[Radarr]
        B[Bazarr]
    end

    subgraph CZ["Captionizerr container"]
        W[Web UI + API]
        Q[Job queue]
        P[Translation pipeline]
        D[(config/)]
    end

    L[(Media library)]
    AI[[AI provider]]
    WH[[Whisper<br/>optional]]
    M[Jellyfin / Emby / Plex]
    N[[ntfy]]

    S -->|webhook| W
    R -->|webhook| W
    W --> Q --> P
    P -->|reads track| L
    P -->|asks for a subtitle| B
    B -->|downloads + syncs| L
    P <-->|translate| AI
    P <-->|audio to words| WH
    P -->|writes .captionizerr.lang.srt| L
    P -->|rescan| M
    P -->|spot-check| N
    Q --- D

    style P fill:#FF9900,color:#fff
    style L fill:#3B48CC,color:#fff
```

- **Sonarr/Radarr** call a webhook on every import, upgrade and rename. The web UI's Library can also queue anything already on disk.
- **The pipeline** reads the subtitle track that ships with the video. Only if there is none, or it stops early, does it ask **Bazarr** to fetch one.
- **Whisper** (optional) turns the audio into words with timestamps, so subtitle timing can be checked against what is actually said.
- **The only thing written into your library** is `<video>.captionizerr.<lang>.srt|ass` (plus `.forced.` for forced tracks), next to the video.
- **Media servers** are told to rescan that one folder, so the track appears within seconds.

## One job, start to finish

```mermaid
flowchart TD
    A([Import webhook]) --> B[Queue job]
    B --> C{Source subtitle?}
    C -->|Embedded text track| E[Extract to cache]
    C -->|File next to video| F[Use it]
    C -->|None| G[Ask Bazarr]
    G --> H[[Waiting for source]]
    H -->|file arrives| C
    E --> X{Track stops early?}
    X -->|No| I{Budget OK?}
    X -->|Yes| Y[Ending from a<br/>complete file]
    Y --> S[Sync to speech]
    F --> S
    S --> I
    I -->|No| J[[Paused: budget]]
    I -->|Yes| K[Translate]
    K --> L[Validate + fix]
    L --> M[Write next to video]
    M --> N([Rescan + notify])

    style K fill:#FF9900,color:#fff
    style J fill:#D13212,color:#fff
```

"Sync to speech" runs only with a Whisper service connected; without one, those sources are used with their own timing.

### Source tracks that stop early

Some releases ship a subtitle track that ends minutes before the video does, with its last line stretched over the rest (seen on WEB-DL CC tracks). A track counts as cut off when its last line lasts over a minute and runs to the end of the video, or when the last quarter of the video has no lines. Then:

1. A complete subtitle in the same language next to the video is used for the **missing ending only**: the embedded track keeps its exact timing up to where it stops. If there is none, Bazarr is asked for one and the job waits up to 3 hours.
2. The ending is lined up with the embedded track on the lines both share in the 5 minutes before the cut (shift and stretch, so a file made for 25 fps fits a 23.976 fps video).
3. With Whisper, the ending is then moved onto the speech, because a file from another release can be edited differently right after the cut, which no subtitle comparison can see.
4. If nothing usable arrives, the job translates what there is, shortens the stretched line and goes to **Needs review**, saying where the source stops.

### Sync to speech

```mermaid
flowchart TD
    A[Audio track<br/>centre channel] --> B[[Whisper:<br/>words + times]]
    C[Subtitle lines] --> D[Match words<br/>in order]
    B --> D
    D --> E[Shift per line]
    E --> F[Drop outliers,<br/>smooth]
    F --> G[Keep the track's<br/>usual lead]
    G --> H([Move lines<br/>that are off])

    style B fill:#FF9900,color:#fff
```

- **Word by word.** Each line's words are found in order in the transcript, so music and effects don't throw it off the way audio-energy methods do.
- **Careful with repeats.** A line counts only when its matched words sit together in the transcript; a phrase repeated from a neighbouring line can't set its time. A line matched far from its neighbours is ignored.
- **The track's own habit is kept.** Most tracks show a line a fraction of a second before it is said; that habit (within −0.6 to +0.3 s) is measured and kept, and lines less than 0.5 s off are not touched, so a well-timed track is left alone and only the parts that are noticeably off move.
- **Where it runs:** by default (`SPEECH_SYNC=auto`) endings taken from another file and downloaded subtitles before translating; `all` adds built-in tracks, `off` leaves only **Re-sync to speech** on a translation's page, which re-times the existing file without re-translating (free; the old timing stays in the editor's history).
- **The service:** any whisper-asr-webservice-compatible server. `deploy/whisper` has a small faster-whisper one; on a 4-core laptop CPU an episode takes a few minutes.

### Inside "Translate" and "Validate + fix"

```mermaid
flowchart TD
    A[Whole episode<br/>in one request] --> B[Parse N-pipe-text lines]
    B --> C{Lines missing?}
    C -->|Yes| D[Ask for those]
    C -->|No| E[Validator]
    D --> E
    E --> F{Flagged lines?}
    F -->|Yes, up to 2 rounds| G[Fix only those]
    G --> E
    F -->|No| H([Done])
    F -->|Still flagged| I([Needs review])

    style A fill:#FF9900,color:#fff
```

- **One request per episode.** The model sees every scene, so it knows who speaks to whom. That gives correct gender and formal or informal address, consistent names, and jokes carried through.
- **Prompt caching.** The style rules and the series notes are cached, so later episodes of a series pay little for them.
- **What the validator checks:** reading speed (short lines may run faster rather than lose words), line length and count, untranslated leftovers, merged or shifted lines, lost positioning tags and italics, and leftover speaker labels. Only flagged lines go back to the model.
- **Upgrades are re-timed, not re-translated.** When a better release replaces a file, the previous translation, including your edits, is matched line by line onto the new release. Only new or changed lines are sent to the model.

## Job states

```mermaid
stateDiagram-v2
    [*] --> queued
    queued --> running
    running --> done
    running --> needs_review
    running --> failed
    running --> skipped: already translated
    running --> no_source
    running --> waiting_source: asked Bazarr
    waiting_source --> queued: subtitle arrived
    running --> paused_budget
    paused_budget --> queued: cap raised / new month
    queued --> batched: backlog (Batch API)
    batched --> running: results ready
    needs_review --> queued: Retry
    failed --> queued: Retry
```

"In progress" on the dashboard covers queued, running, batched and waiting for a source. "Needs attention" covers needs review, failed, no source and paused.

## Backlog at half price

```mermaid
sequenceDiagram
    participant U as You (Library)
    participant C as Captionizerr
    participant A as Anthropic Batch API

    U->>C: Add to backlog
    Note over C: collects jobs for 2 min
    C->>A: one batch, all episodes
    Note over C: estimate reserved<br/>against the budget
    loop every 5 min
        C->>A: done yet?
    end
    A-->>C: results (usually under 1 h)
    C->>C: validate, fix live, write
```

## Where things live

```mermaid
flowchart TD
    subgraph Config["config/ (the container's volume)"]
        DB[(captionizerr.db<br/>jobs, spend, settings)]
        SJ[secrets.json<br/>keys typed in the UI]
        CT[context/<br/>series notes]
        SR[sources/ + cache/<br/>source tracks]
        HI[history/<br/>editor backups]
        BK[backup/<br/>daily DB snapshot]
    end

    style SJ fill:#1A8754,color:#fff
    style DB fill:#3B48CC,color:#fff
```

**To back up:** everything in `config/` except `cache/`, `tmp/` and the live `captionizerr.db*` files. The daily snapshot in `backup/` is the consistent copy of the database. Details: [configuration](configuration.md#files-in-config).

## Where secrets come from

```mermaid
flowchart TD
    A{NAME_FILE set?} -->|Yes| F[Read the file<br/>e.g. compose secret]
    A -->|No| B{NAME in environment?}
    B -->|Yes| E[Use it]
    B -->|No| C{Saved in the UI?}
    C -->|Yes| U[config/secrets.json]
    C -->|No| X([Not configured])

    style F fill:#1A8754,color:#fff
```

- **Server-provided secrets win.** A secret from a file or the environment shows as "Set on the server" in the UI and can't be changed or removed there.
- **UI secrets are write-only.** They are stored with mode 600, never in the database, and never displayed again. The one exception is **Show key** for a webhook key the UI generated itself, so it can be pasted into Sonarr or Radarr by hand.
- **The UI password** is stored as a salted PBKDF2 hash.
- **Never displayed anywhere:** keys don't appear in logs, the HTTP access log (off), API responses or the settings object's `repr`.

## Request authentication

```mermaid
flowchart TD
    A([Incoming request]) --> B{Path}
    B -->|/health /metrics /static| O[Open]
    B -->|/api/hooks/*| K{Webhook key?}
    B -->|everything else| L{Login set?}
    K -->|Basic password, X-Api-Key<br/>or ?apikey=| OK[Allowed]
    K -->|wrong| T[401 + count failure]
    L -->|No| OK
    L -->|Yes| P{Basic auth valid?}
    P -->|Yes| OK
    P -->|No| T
    T -->|10 in 10 min| Z[429 for 15 min]

    style Z fill:#D13212,color:#fff
```

State-changing UI requests must also come from Captionizerr's own pages (Origin/Referer check), which blocks cross-site requests from other browser tabs.
