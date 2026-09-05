# Fork this to watch your own place

This branch is the template. It is the same system as `main` — the one running for
Grad Zavidovići — with every mention of that municipality moved out of the code and
into one file, and with the two things a fork used to have to remember now derived
or measured instead.

The full walkthrough, with the failure modes, is **[docs/firewatch-fork.html](docs/firewatch-fork.html)**.
This is the short path.

## What you need

* A GitHub account. The deployment is Actions + Pages, free on a public repository,
  no card and no server.
* Python 3 with `requests` on your own machine, for the one-off setup.
* A [NASA FIRMS map key](https://firms.modaps.eosdis.nasa.gov/api/map_key/) — free,
  issued by email. Optional: without it Meteosat and Sentinel-3 still run.
* For SMS: an [httpSMS](https://httpsms.com) account and an Android phone as the
  gateway — see below. Also optional. The map works without it.

## The four steps

```bash
# 1. point it at your municipality — writes data/place.json and two data files
python3 -m pip install requests certifi
python3 -m firewatch setup "Općina Kakanj"     # the ADMINISTRATIVE name, not the town

# 2. draw the "nearby" band (the only command needing these two libraries)
python3 -m pip install shapely pyproj
python3 -m firewatch buffer

# 3. edit data/place.json: set "timezone", and the name forms if you are
#    not working in English. setup tells you which lines.

# 4. check, then commit
python3 -m firewatch place
git add data state && git commit -m "watch <your place>"
```

Then, on GitHub: **Settings → Actions → General → Workflow permissions → Read and
write**, **Settings → Pages → Source → GitHub Actions**, and the secrets
`FIRMS_MAP_KEY`, and for SMS `HTTPSMS_API_KEY`, `HTTPSMS_FROM`, `FIREWATCH_SMS_TO`.
Run `poll` once from the Actions tab.

`poll.yml` has no `schedule:` — it is triggered from outside, by anything that can
POST on a timer. That choice, and why, is §08 of the walkthrough.

## Setting up the SMS gateway

httpSMS is not a sending service. There is no per-message fee and no shared short code:
an Android phone *you own* is the gateway, so alerts arrive from your own number at
whatever your carrier charges. An old handset on a charger with a prepaid SIM is the
whole hardware requirement.

1. **Create an account** at <https://httpsms.com>.
2. **Install the Android app** on the phone holding that SIM. It ships as an APK, from
   <https://apk.httpsms.com/HttpSms.apk> or the
   [GitHub releases](https://github.com/NdoleStudio/httpsms/releases/latest).
3. **Sign the app in with a *phone* API key.** Create one at
   <https://httpsms.com/phone-api-keys>, click **VIEW**, scan the QR code with the app.
   A phone key exists only for the app — heartbeats, received messages, and sent, failed
   and delivered events. One phone number is associated with a key at a time; signing in
   with a different key disassociates the previous one.
4. **Take the *account* API key** from <https://httpsms.com/settings>. *That* is
   `HTTPSMS_API_KEY`. `HTTPSMS_FROM` is the gateway phone's own number in E.164,
   matching the number the app registered.

> **The two keys are not interchangeable, and the wrong one fails late.** FireWatch calls
> the full API (`POST /v1/messages/send`), which the phone key is not scoped for. Paste
> the phone key into `HTTPSMS_API_KEY` and nothing complains — `sms-status` reports the
> key as *found*, because it only checks that one is present. You find out when the first
> fire does not text anybody. Run the `test-sms` workflow once with the **send** box
> ticked before relying on it; it is the only check that exercises the whole path.

> **The gateway phone is now part of the alerting system.** It has to stay powered, on the
> network, and with the app running. Android's battery optimisation will stop a background
> app on a handset that has been idle for a week, and the failure is silent from here — a
> 200 from httpSMS means the message was *accepted*, not sent. Exclude the app from
> battery optimisation and leave it on a charger.

API reference: <https://docs.httpsms.com>.

## What this branch changes, and why each one is a trap

**`data/place.json` is the only file that names a place.** The map title and
subtitle, the "nothing detected in ..." line, the reference point every distance and
bearing is measured from, the notification wording, the two committed geometry
files, the timezone and the language the map opens in all read it.
`python3 -m firewatch setup` writes it. Nothing else needs editing.

**The map URL is derived from the repository, not written down.** It used to be a
literal in both workflow files, and left unchanged it is still a *valid, working*
URL — pointing at the map you forked from. Nothing errors; every SMS just quietly
links to somebody else's fires. Both workflows now compute
`https://<owner>.github.io/<repo>` and print what they resolved. Set the repository
*variable* `FIREWATCH_PUBLIC_URL` only for a custom domain.

**`python3 -m firewatch place` is run on every cycle.** A fork that never ran
`setup` is still clipping to Zavidovići, and the symptom of that — an empty map, no
alerts, no SMS — reads exactly like a broken feed or a missing key. One line in the
log settles it. It also reports how much stored history falls outside the configured
clip, which is what tells you the original database is still in `state/`.

**The SMS segment budget is measured, not assumed.** An alert is four lines and one
segment, and the map link is one of those lines — every character of
`https://owner.github.io/repository` is spent before a word of the fire is. With
this repository's URL the longest alert it can produce is 158 characters, two short
of a second segment; with a longer owner and repository name the same alert reaches
170 and costs two segments, for the life of the deployment.
`python3 -m firewatch sms-status` and the `test-sms` workflow both print that worst
case and the headroom left. A shorter repository name is the cheapest fix.

**English SMS wording is written out.** `sms_language` follows `place.json`, and the
English alert kinds and severities are real words rather than the raw keys they used
to fall back to.

## What is still Bosnian

The *language*, which is a different question from the *place*. The map ships with
English and Bosnian; a third language is one block added to `I18N` in `mapgen.py`
(plus one in `SMS_TEXT` for alerts). The Bosnian block also carries a Slavic plural
rule and the `-a → -e` genitive that settlement names take after "od" — if you
delete it and keep English only, nothing else needs touching. `docs/` describes
Zavidovići by name throughout, and is published alongside your map by default.

## Not covered here

The macOS menu bar app (`menubar.py`, `firewatch-ctl`) still carries the launchd
label `com.firewatch.zavidovici`. It reads the place profile like everything else,
so it works; only the service label is cosmetic, and renaming it would strand an
existing install. `docs/firewatch-macos.html` and `docs/firewatch-linux.html` cover
running it off GitHub.
