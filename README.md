# SatTrack

Автоматично приемане на радиолюбителски сателити на 2 m с Yaesu FT-991A. Програмата е **само за приемане**: към радиото отиват единствено команди от разрешен списък (`sattrack/radio/cat.py`), а в кода няма команда за предаване (PTT).

Спецификация: [SatTrackSpec.md](SatTrackSpec.md).

## Инсталация (Windows)

1. Python 3.11 или по-нов. Драйвер Silicon Labs CP210x за радиото.
2. В менюто на радиото: CAT RATE = 38400 (или стойността от `radio.baudrate`).
3. В папката на проекта:

```
python -m venv .venv
.venv\Scripts\pip install -e .[dev]
copy config.example.json config.json
```

4. В `config.json` задайте `radio.port`: COM порта **Enhanced** от Device Manager.

Командата `sattrack` е инсталирана във виртуалната среда `.venv`. Във всеки нов прозорец на PowerShell първо активирайте средата:

```
.venv\Scripts\Activate.ps1
```

Ако PowerShell откаже да изпълни скрипта, разрешете локалните скриптове веднъж с `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`. Без активиране може да се ползва пълният път, например `.venv\Scripts\sattrack passes`.

## Команди

| Команда | Какво прави |
| --- | --- |
| `sattrack update` | Сверява TLE (CelesTrak), честоти (SatNOGS DB) и активност (AMSAT status). |
| `sattrack satellites [--confirm]` | Показва списъка за слушане. `--confirm` го потвърждава (FR-05). |
| `sattrack passes [--hours 24] [--all]` | Показва прогнозата. Със `*` са преминаванията, които ще се записват. |
| `sattrack radio-test [--set-freq 145.8] [--modes]` | Проверява CAT връзката, честотата и кодовете за режими. |
| `sattrack audio-devices` | Показва аудио входовете. Със `*` е този от `recording.device`. |
| `sattrack run [--dry-run]` | Автоматичен режим: клавиш `p` = пауза, `r` = продължи, `s` = прогноза, `q` = изход. |
| `sattrack track <NORAD> [--dry-run]` | Ръчно следене на един сателит (FR-32). |
| `sattrack pause` / `sattrack resume` | Пауза от друг прозорец (файлът `data/PAUSE`). |
| `sattrack rebuild-index` | Възстановява `data/index.json` от отделните JSON файлове. |

Проверка без радио с ускорено време: `sattrack run --dry-run --sim-start 2026-11-21T04:00 --speed 30 --max-passes 1`. Записите от dry-run се пазят в `cache/dryrun/`, извън Git.

## Конфигурация (`config.json`)

Липсващите полета вземат стойностите по подразбиране от `sattrack/config.py`.

- `station`: координати и часова зона.
- `radio`: `port`, `baudrate`, `timeout_s`, `stopbits` (ако отговорите са повредени, опитайте 2), `squelch` — ниво на шумоподтискането по време на пасаж (0 = отворено; след пасажа се връща предишното; `null` = не се пипа). В менюто на радиото **033 CAT RTS трябва да е DISABLE**: програмата държи RTS ниско.
- `band.downlink_ranges_mhz`: разрешени обхвати. За 70 cm добавете `[430.0, 440.0]` (FR-03).
- `passes`:
  - `min_elevation_deg`: минимална максимална височина на преминаването (по подразбиране 30°).
  - `horizon_hours`: колко часа напред се прави прогнозата.
  - `overlap_rule`: правило при припокриване, `max_elevation`, `first` или `longest`.
  - `prestart_s`: колко секунди преди AOS се настройва радиото.
- `doppler.<FM|AM|SSB|CW>`: интервал на обновяване (`interval_s`) и праг за изпращане на нова честота (`threshold_hz`).
- `data`:
  - `refresh_hours`: сверка веднъж на толкова часа.
  - `retry_minutes`: нов опит след неуспешна сверка.
  - `max_age_days` и `max_age_days_narrow`: праг за предупреждение за остарели данни; вторият е за SSB/CW.
  - `activity_hours`: за колко часа назад се броят докладите „чут“ в AMSAT status (по подразбиране 24).
  - `amsat_aliases`: ръчно свързване на NORAD с имена в AMSAT, напр. `{"25544": ["ISS_[FM]"]}`. Нужно е, когато името в TLE не съдържа обозначението от AMSAT (напр. `NO-44`). Свързаните имена се виждат в `sattrack satellites`.
- `selection.auto_count`: колко сателита да избере автоматично.
- `satellites.overrides`: ръчни настройки с предимство пред автоматичните. Полета: `norad_id`, `name`, `frequency_hz`, `mode` (FM, FM-N, AM, USB, LSB, CW, CW-L, DATA-FM, DATA-USB, DATA-LSB), `filter` (`normal`/`narrow`), `cw_pitch_hz`, `enabled`. С `"enabled": false` сателитът се изключва.
- `recording`: `dir`, `device` (част от името на аудио входа), `samplerate`, `channels`, `keep_days` (`null` = записите не се трият).

## Данни

- `data/satellites.json`: текущият списък за слушане и кандидатите. Служи за засичане на промени (FR-16).
- `data/passes/*.json`: по един файл за всяко преминаване. Статусите са `complete`, `incomplete` (рестарт по време на запис), `aborted` (пауза или изход).
- `data/index.json`: общ индекс на преминаванията.
- `recordings/*.wav`, `cache/`, `logs/sattrack.log`: не влизат в Git.

## Тестове

```
.venv\Scripts\python -m pytest
```
