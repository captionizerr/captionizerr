# Whisper service for CaptionizeRR

A small speech-to-text server built on [faster-whisper](https://github.com/SYSTRAN/faster-whisper).
CaptionizeRR uses it to sync subtitles to the speech (Settings → Connections → Whisper). It speaks
the subset of the [whisper-asr-webservice](https://github.com/ahmetoner/whisper-asr-webservice) API
that CaptionizeRR and Bazarr's Whisper provider use, so any of those servers works as well.

## Install (Debian 12/13, a VM or an LXC)

```sh
sh install.sh 192.0.2.10      # the IPs allowed to reach port 9000 (your CaptionizeRR / Bazarr host)
```

It installs ffmpeg and a Python venv in `/opt/whisper`, a `whisper` systemd service on port 9000 and,
when IPs are given, an nftables rule that only lets those reach it. The first start downloads the
model (`small`, about 500 MB) to `/opt/whisper/models`.

Then set the address in CaptionizeRR: `http://<host>:9000` (or `CAPTIONIZERR_WHISPER_URL`).

## Sizing

CPU only. With the `small` model and int8 on a 4-core/8-thread laptop-class CPU (i7-8809G), 3 minutes
of audio take about 20 s, so a 60-minute episode takes about 7 minutes. Give it 4 cores and 4–6 GB
of RAM. Change the model with `WHISPER_MODEL` in `whisper.service` (`base` is faster, `medium` more
accurate and about 3× slower).

## API

- `POST /asr?language=en&output=json&word_timestamps=true` with the audio as multipart `audio_file`
  → `{text, language, segments: [{start, end, text, words: [{word, start, end, probability}]}]}`
  (`output=srt|vtt|txt` also work)
- `POST /detect-language` → `{detected_language, language_code, confidence}`
- `GET /health` → `{ok, model}`

There is no authentication: keep it on your LAN and restrict who can reach it (the install script's
firewall rule does that).
