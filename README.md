# NHK BSP4K XMLTV EPG

This repository generates an XMLTV EPG for `ＮＨＫ ＢＳＰ４Ｋ` from bangumi.org.

The GitHub Actions workflow fetches schedules from today through the next 8 days, writes:

- `nhk-bsp4k.xml`
- `nhk-bsp4k.xml.gz`

and publishes both files to the `nhk-bsp4k-latest` release.

Run locally:

```sh
python scripts/fetch_nhk_bsp4k_epg.py --days 8
```
