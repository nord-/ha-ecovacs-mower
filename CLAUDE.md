# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Vad projektet är

Home Assistant-custom integration (`custom_components/ecovacs_mower`) för Ecovacs GOAT-gräsklippare. Den är en **fork av HA cores `ecovacs`-integration** med XMPP/legacy-stödet bortklippt (MQTT-only) plus ett patchlager som rättar tre fel i `deebot-client` som gör GOAT ostyrbar. Se README.md för felbeskrivningarna och länkar till uppströms-PR:erna.

## Kommandon

```bash
# Hela sviten — kräver Linux/CI
python -m pytest tests/ -v

# Enskild fil / test
python -m pytest tests/test_sensor.py -v
python -m pytest tests/test_sensor.py::test_name -v

# Lokalt på Windows: bara protokoll-lagret går att köra
python -m pytest tests/deebot_patch/ -p no:homeassistant -v

pip install -r requirements-test.txt
```

**Home Assistant går inte att importera på Windows** (`homeassistant/runner.py` gör ett oskyddat `import fcntl`). Därför:

- Allt som importerar HA ligger i en fil märkt `pytestmark = requires_ha` (från `tests/__init__.py`).
- `-p no:homeassistant` krävs lokalt eftersom `pytest_homeassistant_custom_component` autoladdas som entry point — flaggan hör **inte** hemma i `pytest.ini`, CI behöver pluginet.
- Sanningskällan för testresultat är CI (`.github/workflows/test.yml`, ubuntu-latest, Python 3.14). Påstå aldrig att sviten är grön utifrån en Windows-körning.

CI kör dessutom hassfest och HACS-validering (`.github/workflows/hassfest.yml`). HACS-jobbets `topics`/`brands`-fel är förväntade — de gäller bara listning i default-storen.

## Arkitektur

### Patchlagret är den enda kopplingen till deebot-clients internals

`deebot_patch/` är avgränsningen: **ingen annan modul får röra privata delar av `deebot_client`** (`_DEVICES`, `MESSAGES`). Byts biblioteket mot en vendrad klient skrivs bara den mappen om.

- `commands.py` — `CleanMower`, ärver `Clean` (topic `clean`) med V2-nyttolast. Ersätter `CleanV2` som publicerar på `clean_V2`, vilket GOAT-firmware ignorerar.
- `hardware.py` — `patch_device_info()` såddar `_DEVICES`-cachen med rättade `Capabilities` (`CleanMower` + `GetCleanInfo` istället för `GetCleanInfoV2`). Använder bibliotekets egen cachemekanism istället för monkeypatch. `SUPPORTED_CLASSES` listar verifierade enhetsklasser (`2i0fns` = O1200 LiDAR Pro).
- `messages.py` — `OnChargeInfo` och `OnScheduleTaskInfo`, de två oombedda meddelanden biblioteket saknar handler för.
- `__init__.py` — `apply()` (registrerar meddelandena, idempotent) och `verify_capabilities()`.

### Ordningen i `EcovacsController.initialize()` är en hård invariant

```
apply() → patch_device_info(varje SUPPORTED_CLASS) → get_devices() → verify_capabilities()
```

`get_devices()` bakar in kapabiliteterna i `DeviceInfo.static`, en frozen dataclass. Patchar man efteråt har enheterna redan fått de opatchade. `verify_capabilities()` kontrollerar därför **objektet enheten faktiskt fick**, inte cachen — en cacheuppslagning skulle se rätt ut ändå.

Fail-fast är avsiktligt: ser `deebot-client` inte ut som patchlagret förväntar sig kastas `PatchContractError` → `ConfigEntryError`, och integrationen vägrar starta hellre än att tyst sluta rapportera klipparens tillstånd. `tests/deebot_patch/test_contract.py` fångar samma antaganden i CI.

### Entitetsplattformar

`lawn_mower` filtrerar på `device_type is DeviceType.MOWER`. De övriga (`sensor`, `switch`, `number`, `button`, `event`) byggs deklarativt: en `ENTITY_DESCRIPTIONS`-tuple av `EcovacsCapabilityEntityDescription`-subklasser med `capability_fn`, matad genom `util.get_supported_entities()`. Nya entiteter läggs till som en post i den tuplen — inte som en ny klass.

`entity.py` har basklasserna (`EcovacsEntity`, `EcovacsDescriptionEntity`); prenumeration på events sker via `_subscribe()` i `async_added_to_hass`.

## Konventioner

- **Docstrings och kommentarer skrivs på svenska**, kod-identifierare på engelska. Forkade moduler inleder sin docstring med vad som togs bort jämfört med core.
- Kommentarer förklarar *varför*, särskilt där koden ser onödigt omständlig ut (exakt typjämförelse istället för `isinstance`, mutation på plats istället för ombindning). Ta inte bort dem för att "städa".
- `strings.json` och `translations/en.json` ska vara **identiska** — `test_translations.py` vaktar det, ingen automatik synkar dem. Skapa aldrig en `sv.json`; HA-frontendens språk är engelska här.
- Varje translation-nyckel och `icons.json`-nyckel ska höra till en verklig entitet — plattformstesterna kontrollerar båda riktningarna.
- Ny hårdvara stöds genom att lägga till enhetsklassen i `SUPPORTED_CLASSES`. Icke-stödda MOWER-klasser loggar en warning med klassträngen; det är den sträng användare ombeds rapportera.
- Version bumpas i `manifest.json`. `deebot-client` är hårt pinnad där och i `requirements-test.txt` — håll dem lika.
- Conventional commits, inga AI-attributioner.
