# Zendure-zenSDK-proxy

> Credit: deze AppDaemon versie bouwt voort op de originele
> [`Zendure-zenSDK-proxy`](https://github.com/gast777/Zendure-zenSDK-proxy)
> van Casper Rijnders / `gast777`.

Deze repo is op dit moment experimenteel. Bijdrages (ook door middel van testen) worden gewaardeerd!

## Inhoud

- [Credits, upstream en wijzigingen](#credits-upstream-en-wijzigingen)
- [Robuust omgaan met uitval van Zendures](#robuust-omgaan-met-uitval-van-zendures)
- [AppDaemon via HACS](#appdaemon-via-hacs)
  - [Installatie](#installatie)
  - [AppDaemon configuratie](#appdaemon-configuratie)
  - [Home Assistant automations](#home-assistant-automations)
  - [Pushmelding bij degraded pool](#pushmelding-bij-degraded-pool)
  - [Logging](#logging)
  - [Metrics](#metrics)
  - [Queue model](#queue-model)
  - [Testen vanuit HA Terminal](#testen-vanuit-ha-terminal)
  - [Overstappen van Node-RED naar AppDaemon](#overstappen-van-node-red-naar-appdaemon)
  - [Release naar HACS](#release-naar-hacs)
- [Bewust verschil met Node-RED bij drie Zendures](#bewust-verschil-met-node-red-bij-drie-zendures)

## Credits, upstream en wijzigingen

Deze repository is gebaseerd op de originele
[`gast777/Zendure-zenSDK-proxy`](https://github.com/gast777/Zendure-zenSDK-proxy)
van Casper Rijnders.

De originele repository levert een Node-RED proxy voor Gielz/Home Assistant en
Zendure devices. Die Node-RED proxy verzorgt de interfacing naar meerdere
Zendure devices, combineert de status naar een virtueel device, verdeelt laad-
en ontlaadvermogen, en voegt extra monitoring-attributen toe voor Home
Assistant.

Deze fork voegt een AppDaemon/Python implementatie toe. De bestandslijst van de
AppDaemon/Python toevoeging staat in [`LICENSE`](LICENSE).

Dank aan `gast777` voor het uitzoeken van de Zendure-interfacing, de
proxy-aanpak, de vermogensverdeling, de monitoring-attributen, de Home Assistant
voorbeelden, en de vele praktijktests waarop deze AppDaemon versie voortbouwt.

Deze AppDaemon/Python implementatie van de proxy heeft vijf concrete doelen, bovenop die van de upstream repo:

1. Backwards compatible blijven met de proxy van `gast777`. Bestaande Gielz
   dashboards, Home Assistant sensors, en REST aanroepen moeten dezelfde
   proxy-response kunnen blijven gebruiken.
2. Installeren en updaten via HACS. Een gebruiker kan de AppDaemon versie via
   HACS installeren en later via HACS upgraden, zonder handmatig een Node-RED
   export te importeren.
3. Korte Zendure uitval zachter afhandelen. De Gielz integratie blijft waar
   mogelijk gewoon werken, en de proxy blijft de vermogensvraag zo goed mogelijk
   volgen wanneer één Zendure tijdelijk traag of onbereikbaar is. Zie
   [Robuust omgaan met uitval van Zendures](#robuust-omgaan-met-uitval-van-zendures).
4. Opstoppingen en overbelasting na korte uitval voorkomen. De proxy voorkomt dat Zendures na een
   hapering een stapel oude of dubbele opdrachten krijgen, zodat de aansturing
   rustiger blijft, en verdere haperingen worden voorkomen. Zie [Queue model](#queue-model).
5. Problemen per Zendure zichtbaar maken. De metrics laten zien hoe vaak en
   wanneer een specifieke Zendure traag reageert, timeouts heeft, of fouten
   geeft. Zie [Metrics](#metrics).

De Node-RED bestanden, de documentatie over de originele proxy-aanpak, en de
Home Assistant voorbeelden blijven afkomstig van de upstream repository van
`gast777`, behalve waar deze fork expliciet AppDaemon/HACS documentatie toevoegt.
De Node-RED implementatie blijft ongewijzigd en wordt gebruikt als
referentiepunt voor REST-velden, Gielz-compatibiliteit, en bestaande Home
Assistant voorbeelden.

## Robuust omgaan met uitval van Zendures

De originele Node-RED versie van `gast777` blijft de basis voor de proxy-aanpak:
meerdere fysieke Zendures worden zichtbaar als één virtuele Zendure voor Gielz
en Home Assistant. De AppDaemon/Python versie in `apps/Zendure-zenSDK-proxy/`
voegt extra gedrag toe voor situaties waarin één fysieke Zendure traag wordt of
niet meer antwoordt.

Ook met één Zendure kan de AppDaemon/Python proxy nuttig zijn. Korte haperingen
komen in de praktijk voor bij een wissel tussen laden en ontladen, bij interne
cronjobs op de Zendure, of bij kortstondige packetloss en vertraging in de
draadloze Wi-Fi verbinding van de Zendures. De proxy zorgt ervoor dat korte
verbindingsproblemen, timeouts, en onvolledige Zendure responses niet meteen resulteren in een harde Home Assistant fout, en verlies van controle over de overige Zendures. De proxy gebruikt
`ha_get_response_timeout`, `zendure_request_timeout`, `get_cache_max_age`, en
`get_recovery_window` om Home Assistant snel antwoord te geven, late Zendure
responses alsnog in de cache te bewaren, en POST opdrachten pas te hervatten
wanneer de Zendure weer stabiele GET responses geeft. Zo worden korte haperingen
in Wi-Fi, Home Assistant, of de Zendure zelf graceful afgehandeld in plaats van
dat een directe REST call vanuit een Home Assistant automation direct faalt.
Zulk foutgedrag nabouwen in een Home Assistant automation is nagenoeg
onmogelijk, omdat een automation weinig controle heeft over per-request
timeouts, late responses, cachegebruik, en herstelvensters per Zendure.

In gewone taal:

- Home Assistant REST calls falen vaak op een timeout na ongeveer 10 seconden.
  De AppDaemon versie probeert daarom binnen `ha_get_response_timeout`,
  standaard 8 seconden, een antwoord aan Home Assistant te geven. Ook wanneer een (of meerdere) Zendures nog geen antwoord heeft gegeven.
- Een trage Zendure request wordt intern niet meteen afgebroken. `DeviceClient`
  gebruikt `zendure_request_timeout`, standaard 60 seconden. Als de Zendure later
  alsnog antwoordt, schrijft de proxy die response naar de cache.
- Als Home Assistant eerder antwoord nodig heeft, stuurt de proxy de laatste
  bekende goede GET response terug zolang `get_cache_max_age`, standaard 300
  seconden, nog niet verlopen is.
  Daardoor blijft de proxy response bruikbaar in Home Assistant bij een korte
  vertraging of korte uitval van één Zendure, in plaats van dat de volledige
  proxy response meteen `unavailable` wordt.
- `sensor.proxy_zendure_pool_healthy` is `Healthy` wanneer alle geconfigureerde
  Zendures normaal antwoorden. De sensor is `Degraded` wanneer één of meer
  Zendures niet goed antwoorden.
- `sensor.zendure_1_health`, `sensor.zendure_2_health`, en
  `sensor.zendure_3_health` tonen per Zendure `Healthy`, `Degraded`, of `Dead`.
- Een Zendure wordt `Degraded` wanneer een outgoing GET naar die Zendure geen
  bruikbare response oplevert. Voorbeelden zijn: geen antwoord binnen
  `zendure_request_timeout`, standaard 60 seconden, een verbroken verbinding,
  een HTTP fout, of een response die de proxy niet kan verwerken.
- Een `Degraded` Zendure krijgt geen POST opdrachten meer. De proxy gaat er
  tijdelijk wel vanuit dat die Zendure blijft laden of ontladen met het wattage
  uit de laatste succesvolle GET response. Daardoor kan de proxy het resterende
  gevraagde wattage naar de gezonde Zendures sturen.
  Het overslaan van POST opdrachten naar een `Degraded` Zendure ontlast die
  Zendure, zodat herstel meer kans krijgt. Het overslaan voorkomt ook dat oude
  POST opdrachten voor die Zendure opstapelen terwijl de verbinding slecht is.
- `degraded_power_hold_seconds`, standaard 1800 seconden, bepaalt hoe lang de
  proxy die laatste bekende vermogensstand meetelt. Na die periode wordt de Zendure
  `Dead`, en de proxy gaat er dan vanuit dat die Zendure geen vermogen meer
  levert of opneemt.
- Wanneer een uitgevallen Zendure weer succesvolle GET responses geeft, wacht de
  proxy `get_recovery_window`, standaard 30 seconden, voordat die Zendure weer
  POST opdrachten krijgt. Dan is deze Zendure weer `Healthy`.

Voorbeeld: Home Assistant vraagt 1600 W laden. Zendure 2 is `Degraded`, en de
laatste succesvolle GET response meldde dat Zendure 2 nog met 500 W laadt. De
proxy stuurt dan geen POST naar Zendure 2. De proxy verdeelt de resterende
1100 W over Zendure 1 en Zendure 3, zodat de totale aansturing zo dicht mogelijk
bij 1600 W blijft.

## AppDaemon via HACS

HACS installeert de AppDaemon code uit `apps/Zendure-zenSDK-proxy/`. HACS maakt geen AppDaemon installatie aan en HACS past `apps.yaml` niet automatisch aan. Installeer daarom eerst AppDaemon en voeg daarna de configuratie uit `examples/apps.yaml` toe aan je AppDaemon `apps.yaml`.

### Installatie

1. Installeer en start AppDaemon in Home Assistant.
2. Open HACS.
3. Open de HACS configuratie-opties.
4. Zet `Enable AppDaemon apps discovery & tracking` aan.
5. Ga naar `Custom repositories`.
6. Voeg deze GitHub repository toe als type `AppDaemon`.
7. Installeer `Zendure zenSDK Proxy`.
8. Open je AppDaemon `apps.yaml`.
9. Kopieer de `zendure_proxy` configuratie uit [`examples/apps.yaml`](examples/apps.yaml) naar je AppDaemon `apps.yaml`.
10. Vul `ip_zendure_1`, `ip_zendure_2`, en optioneel `ip_zendure_3` in.
11. Herstart AppDaemon.
12. Vul in Gielz bij `Zendure 2400 AC IP-adres` het interne AppDaemon add-on adres in: `a0d7b954-appdaemon:8120/endpoint`.

HACS downloadt de AppDaemon code naar de Home Assistant configuratiemap onder `appdaemon/apps/`.

Als AppDaemon als Home Assistant add-on draait, gebruik dan vanuit Home Assistant Core de interne add-on hostnaam `a0d7b954-appdaemon`. Gebruik `localhost:8120` alleen wanneer de caller in dezelfde container als AppDaemon draait.

Na een HACS update van `Zendure zenSDK Proxy`: herstart AppDaemon. HACS vervangt de Python bestanden, maar HACS herstart de AppDaemon add-on niet zelf.

### AppDaemon configuratie

De AppDaemon entry point is `apps/Zendure-zenSDK-proxy/zendure_proxy.py`.

De AppDaemon class is `ZendureProxy`.

De AppDaemon module naam is `zendure_proxy`.

Volledige configuratie voorbeeld:

```yaml
zendure_proxy:
  module: zendure_proxy
  class: ZendureProxy

  ip_zendure_1: "192.168.1.101"
  ip_zendure_2: "192.168.1.102"
  ip_zendure_3: ""

  server_host: "0.0.0.0"
  server_port: 8120
  zendure_request_timeout: 60
  ha_get_response_timeout: 8
  get_cache_max_age: 300
  get_rate_limit_window: 1
  get_recovery_window: 30
  degraded_power_hold_seconds: 1800

  single_mode_upperlimit_percent: 100
  single_mode_lowerlimit_percent: 40
  single_mode_change_device_diff: 5

  single_mode_delayed_standby_timer: 300
  single_mode_standby_charging_enable: true
  single_mode_standby_discharging_enable: true

  singlemode_transition_timer: 40

  balancing_factor: 5

  dualmode_damper_enable: false
  dualmode_damper_timer: 120
  dualmode_damper_amount: 200

  always_dual_mode: false
  equal_mode: false

  solar_power_info: false
  manual_mode_repeat: true

  log_file_enabled: true
  log_file_path: ""
  log_file_max_bytes: 1000000
  log_file_backup_count: 5

  log_dashboard_enabled: true
  log_dashboard_route: "zendure_proxy_logs"
  log_dashboard_lines: 300

  metrics_enabled: true
  metrics_dashboard_enabled: true
  metrics_dashboard_route: "zendure_proxy_metrics"
  metrics_dashboard_refresh: 10
  metrics_ha_sensors_enabled: true
  metrics_ha_sensors_interval: 30

  proxy_ha_sensors_enabled: true
  proxy_ha_sensors_skip_existing: true
  proxy_ha_sensors_mqtt_discovery_enabled: true
  proxy_ha_sensors_mqtt_discovery_prefix: "homeassistant"
  proxy_ha_sensors_mqtt_state_prefix: "zendure_proxy"
  proxy_ha_sensors_mqtt_retain: true
```

Voor een eerste test hoef je meestal alleen `ip_zendure_1`, `ip_zendure_2`, `ip_zendure_3`, `server_host`, en `server_port` aan te passen. De overige waarden hierboven zijn de standaardwaarden uit [`examples/apps.yaml`](examples/apps.yaml).

De proxy luistert daarna op de legacy HTTP URLs:

```text
http://<appdaemon-host>:8120/properties/report
http://<appdaemon-host>:8120/properties/write
http://<appdaemon-host>:8120/endpoint/properties/report
http://<appdaemon-host>:8120/endpoint/properties/write
```

Gebruik in Gielz op Home Assistant OS of Home Assistant Supervised meestal:

```text
a0d7b954-appdaemon:8120/endpoint
```

Gebruik in Gielz alleen een gewone hostnaam of IP-adres wanneer de AppDaemon poort `8120` ook buiten de add-on container bereikbaar is:

```text
<appdaemon-host>:8120/endpoint
```

### Home Assistant automations

Home Assistant Core kan AppDaemon add-ons bereiken via de interne add-on DNS naam. Voor de AppDaemon add-on uit de Home Assistant Community Add-ons repository is die hostnaam meestal:

```text
a0d7b954-appdaemon
```

Voor bestaande Gielz automations is meestal geen aparte `rest_command` nodig. Vul bij `Zendure 2400 AC IP-adres` deze waarde in:

```text
a0d7b954-appdaemon:8120/endpoint
```

Gebruik deze `rest_command` configuratie alleen wanneer een eigen Home Assistant automation rechtstreeks de AppDaemon API endpoints aanroept:

```yaml
rest_command:
  zendure_proxy_report:
    url: "http://a0d7b954-appdaemon:5050/api/appdaemon/zendure_proxy_report"
    method: GET

  zendure_proxy_write:
    url: "http://a0d7b954-appdaemon:5050/api/appdaemon/zendure_proxy_write"
    method: POST
    content_type: "application/json"
    payload: "{{ payload }}"
```

Een automation kan daarna `rest_command.zendure_proxy_write` aanroepen met JSON in `payload`.

Voorbeeld:

```yaml
action: rest_command.zendure_proxy_write
data:
  payload: '{"properties":{"outputHomePower":1200}}'
```

De AppDaemon endpoints zijn:

```text
GET  http://a0d7b954-appdaemon:5050/api/appdaemon/zendure_proxy_report
POST http://a0d7b954-appdaemon:5050/api/appdaemon/zendure_proxy_write
```

De oude URLs op `8120` blijven bestaan voor installaties waarin de caller de AppDaemon containerpoort direct kan bereiken.

### Pushmelding bij degraded pool

Het is aan te raden om in Home Assistant een automation te maken die een push
message stuurt wanneer `sensor.proxy_zendure_pool_healthy` naar `Degraded` gaat.
Die melding geeft je de kans om de fysieke Zendure, het IP-adres, Wi-Fi, of de
stroomvoorziening te controleren voordat de proxy de Zendure als `Dead`
behandelt.

Voorbeeld:

```yaml
alias: Zendure pool degraded melding
mode: single
trigger:
  - platform: state
    entity_id: sensor.proxy_zendure_pool_healthy
    to: "Degraded"
    for: "00:01:00"
action:
  - service: notify.mobile_app_jouw_telefoon
    data:
      title: "Zendure proxy degraded"
      message: >
        Een of meer Zendures reageren niet goed. Controleer
        sensor.zendure_1_health, sensor.zendure_2_health, en
        sensor.zendure_3_health in Home Assistant.
```

### Logging

`ZendureProxy` schrijft eigen logregels naar de standaard AppDaemon log en naar een roterende logfile.

Standaard logfile:

```text
<appdaemon-config-dir>/logs/zendure_proxy.log
```

Laat `log_file_path` leeg om deze standaardlocatie te gebruiken. Vul `log_file_path` alleen wanneer je zelf een ander absoluut pad wilt gebruiken.

De roterende logfile gebruikt deze instellingen:

```yaml
log_file_enabled: true
log_file_path: ""
log_file_max_bytes: 1000000
log_file_backup_count: 5
```

`log_file_max_bytes` bepaalt de maximale grootte van `zendure_proxy.log`. `log_file_backup_count` bepaalt hoeveel oude bestanden bewaard blijven, zoals `zendure_proxy.log.1` en `zendure_proxy.log.2`.

De AppDaemon UI logpagina gebruikt deze instellingen:

```yaml
log_dashboard_enabled: true
log_dashboard_route: "zendure_proxy_logs"
log_dashboard_lines: 300
```

Open de logpagina via de AppDaemon UI:

```text
http://a0d7b954-appdaemon:5050/app/zendure_proxy_logs
```

Open dezelfde logpagina vanuit een browser op je laptop via het IP-adres of de hostname van Home Assistant:

```text
http://<home-assistant-host>:5050/app/zendure_proxy_logs
```

De logpagina toont de laatste `log_dashboard_lines` regels en heeft een downloadlink voor de huidige logfile plus de rotatiebestanden.

De proxy schrijft waarschuwingen wanneer queue cleanup wordt uitgevoerd:

```text
Queue cleanup: coalesced 3 queued GET requests into 1 upstream GET
Queue cleanup: deduplicated 2 queued POST requests
```

De eerste waarschuwing betekent dat meerdere wachtende GET requests hetzelfde gecombineerde Zendure antwoord krijgen. De tweede waarschuwing betekent dat meerdere wachtende POST requests met dezelfde property keys zijn teruggebracht tot de nieuwste POST request.

### Metrics

`ZendureProxy` houdt metrics in memory bij voor inkomende Home Assistant requests, queue cleanup en uitgaande Zendure requests.

De metrics configuratie staat standaard aan:

```yaml
metrics_enabled: true
metrics_dashboard_enabled: true
metrics_dashboard_route: "zendure_proxy_metrics"
metrics_dashboard_refresh: 10
metrics_ha_sensors_enabled: true
metrics_ha_sensors_interval: 30
```

De proxy response sensors staan ook standaard aan:

```yaml
proxy_ha_sensors_enabled: true
proxy_ha_sensors_skip_existing: true
proxy_ha_sensors_mqtt_discovery_enabled: true
proxy_ha_sensors_mqtt_discovery_prefix: "homeassistant"
proxy_ha_sensors_mqtt_state_prefix: "zendure_proxy"
proxy_ha_sensors_mqtt_retain: true
```

Open het metrics dashboard via de AppDaemon UI:

```text
http://a0d7b954-appdaemon:5050/app/zendure_proxy_metrics
```

Open hetzelfde metrics dashboard vanuit een browser op je laptop via het IP-adres of de hostname van Home Assistant:

```text
http://<home-assistant-host>:5050/app/zendure_proxy_metrics
```

Deze log- en metrics-pagina's zijn AppDaemon app routes. `register_route(...)` publiceert deze routes onder `/app/<route>`, maar AppDaemon zet app routes niet automatisch in de HADashboard lijst.

Wil je `Zendure Proxy` wel in de AppDaemon HADashboard lijst zien, kopieer dan [`examples/zendure_proxy.dash`](examples/zendure_proxy.dash) naar je AppDaemon `dashboards` map en herstart AppDaemon of forceer een dashboard recompile. Volgens de AppDaemon documentatie zoekt HADashboard standaard naar `.dash` bestanden in de `dashboards` map onder de AppDaemon config directory.

Het metrics dashboard toont:

- uptime van de proxy;
- inkomende GET/POST totalen;
- inkomende GET/POST error rates over de laatste 5 minuten;
- inkomende GET/POST gemiddelde latency, p95 latency en max latency;
- inkomende queue depths;
- aantal GET requests dat door coalescing is bespaard;
- aantal POST requests dat door deduplicatie is overgeslagen;
- per Zendure device de uitgaande GET/POST totalen;
- per Zendure device de uitgaande GET/POST error rates over de laatste 5 minuten;
- per Zendure device de uitgaande GET/POST gemiddelde latency, p95 latency en max latency;
- per Zendure device de uitgaande queue depth.

De proxy publiceert standaard ook Home Assistant metrics via AppDaemon `set_state()`. De metrics worden elke `metrics_ha_sensors_interval` seconden bijgewerkt.

`ZendureProxy._publish_metrics_sensors()` publiceert de queue, latency en error metrics. `ZendureProxy._restore_metrics_counters_from_ha()` leest counter sensor states uit Home Assistant bij het starten van AppDaemon. Daardoor tellen `sensor.zendure_proxy_incoming_get_total`, `sensor.zendure_proxy_queue_get_coalesced_total`, en de andere `*_total` counters verder vanaf de laatste Home Assistant state na een AppDaemon restart.

`MetricsRegistry.flat_ha_sensors()` zet `state_class: total_increasing` alleen op counter sensors. Gewone meetwaarden zoals p95 latency, queue depth en error rate krijgen geen `state_class: total_increasing`.

Voorbeelden van sensors:

```text
sensor.zendure_proxy_uptime
sensor.zendure_proxy_incoming_get_p95_ms
sensor.zendure_proxy_incoming_post_p95_ms
sensor.zendure_proxy_incoming_get_total
sensor.zendure_proxy_incoming_post_total
sensor.zendure_proxy_incoming_get_error_rate
sensor.zendure_proxy_incoming_post_error_rate
sensor.zendure_proxy_queue_get_depth
sensor.zendure_proxy_queue_post_depth
sensor.zendure_proxy_queue_cleanup_total
sensor.zendure_proxy_queue_get_coalesced_total
sensor.zendure_proxy_queue_post_deduplicated_total
sensor.zendure_proxy_device_1_queue_depth
sensor.zendure_proxy_device_1_get_p95_ms
sensor.zendure_proxy_device_1_post_p95_ms
sensor.zendure_proxy_device_1_error_rate
```

Voor een 2- of 3-device setup worden ook `device_2` en `device_3` sensors aangemaakt.

De proxy kan de gewone proxy sensors automatisch in Home Assistant aanmaken. Dat zijn dezelfde soort sensors als de oude REST sensors uit `HA_REST_proxy_sensors_NL` en `HA_REST_proxy_sensors_EN`, maar dan zonder dat je dat sensorblok handmatig hoeft te plakken.

De proxy probeert eerst MQTT discovery. Als MQTT discovery werkt, krijgen de sensors ook een `unique_id`. Daardoor kun je de sensors in Home Assistant via de UI beheren, bijvoorbeeld hernoemen of aan een gebied koppelen.

Kort gezegd: `entity_id` is de naam die je in dashboards en automations ziet, zoals `sensor.zendure_2_serienummer`. `unique_id` is het vaste interne nummer waarmee Home Assistant weet dat dezelfde sensor na een herstart of update nog steeds dezelfde sensor is. Zonder `unique_id` kan Home Assistant de sensorwaarde wel tonen, maar kun je de sensor meestal niet netjes beheren via de UI.

MQTT discovery werkt alleen wanneer je Home Assistant installatie MQTT heeft. Je hebt daarvoor een MQTT broker nodig, de Home Assistant MQTT integration, en de AppDaemon MQTT plugin. Niet iedere installatie heeft MQTT al ingesteld.

Heeft jouw installatie geen MQTT, dan maakt de proxy de sensors alsnog aan via AppDaemon. Die sensors werken dan gewoon voor dashboards en automations, maar Home Assistant toont bij die sensors geen `unique_id`. Dat is een beperking van Home Assistant: een gebruiker kan zelf geen `unique_id` toevoegen aan een entity die zonder `unique_id` is aangemaakt.

Wil je zeker `unique_id` zonder MQTT? Gebruik dan de oude REST sensor YAML. De automatische AppDaemon fallback is vooral bedoeld om sensorwaarden zonder handmatige installatie beschikbaar te maken.

Voorbeelden van proxy response sensors:

```text
sensor.zendure_2_soc_limiet_status
sensor.zendure_2_serienummer
sensor.dual_mode_demper_status
sensor.vermogensopdracht_zendure_2
sensor.zendure_actief_device
sensor.zendure_proxy_versie
```

`proxy_ha_sensors_skip_existing: true` betekent: als je de oude REST sensors al hebt, laat de proxy die bestaande sensors met rust. Daardoor blijven bestaande installaties werken na een update.

Nieuwe installaties zonder oude REST sensor YAML krijgen de proxy sensors automatisch. Als MQTT werkt, krijgen de nieuwe sensors een `unique_id`. Als MQTT niet werkt, krijgen de nieuwe sensors geen `unique_id`, maar de sensorwaarden komen wel binnen.

Wil je op een bestaande installatie overstappen van oude REST sensors naar automatische MQTT discovery sensors, verwijder dan eerst de oude REST sensor YAML voor dezelfde entity_id's uit Home Assistant. Verwijder daarna oude REST sensor entity registry entries wanneer Home Assistant de oude REST entities als unavailable laat staan. Gebruik niet tegelijk oude REST sensors en MQTT discovery sensors voor dezelfde entity_id's, want Home Assistant maakt dan dubbele namen zoals `sensor.zendure_2_serienummer_2`.

De metrics code staat in `zendure_proxy_metrics.py`. De proxy response sensor code staat in `zendure_proxy_ha_sensors.py`.

### Queue model

De proxy heeft twee lagen voor requestverwerking. De eerste laag verwerkt
inkomende Home Assistant requests. De tweede laag verwerkt uitgaande HTTP
requests naar de fysieke Zendure devices.

`RequestQueue` in `zendure_proxy_queue.py` verwerkt inkomende Home Assistant
requests. Een GET request vraagt de actuele Zendure status op via
`/properties/report`. Wanneer Home Assistant meerdere GET requests tegelijk
stuurt, wacht `RequestQueue` tot de worker één upstream GET ronde heeft gedaan.
Daarna krijgen alle wachtende GET requests dezelfde response. Dat voorkomt dat
een dashboard reload, meerdere REST sensors, of een korte timeout direct meerdere
bijna gelijke GET rondes naar de Zendures stuurt.

Een POST request stuurt een opdracht naar de Zendure devices via
`/properties/write`. Wanneer meerdere POST requests wachten met dezelfde
property keys, bewaart `RequestQueue` alleen de nieuwste payload voor die keys.
Voorbeeld: drie wachtende POST requests met `inputLimit` worden teruggebracht
tot één POST request met de nieuwste `inputLimit` waarde. De oudere wachtende
POST requests krijgen direct `{"ack":"pong"}` terug. Dat voorkomt dat een oude
opdracht alsnog wordt uitgevoerd nadat Home Assistant al een nieuwere opdracht
heeft gestuurd.

Elke `DeviceClient` in `zendure_proxy_device_client.py` heeft een eigen
uitgaande `asyncio.Queue`. Er is dus een aparte uitgaande queue per fysieke
Zendure. De worker van `DeviceClient` stuurt maximaal één request tegelijk naar
hetzelfde Zendure IP-adres. Dat voorkomt overlappende requests naar dezelfde
Zendure wanneer de Zendure traag reageert door Wi-Fi vertraging, packetloss, een
interne Zendure taak, of een wissel tussen laden en ontladen.

`zendure_proxy_health.py` en de GET cache bepalen wat er gebeurt wanneer een
Zendure langer traag blijft. Wanneer een GET ronde te lang duurt voor Home
Assistant, geeft `_execute_report_request(...)` een geldige cached response
terug zolang `get_cache_max_age` dat toestaat. Wanneer een Zendure geen
bruikbare GET response geeft, markeert `zendure_proxy_health.py` die Zendure als
`Degraded`. `execute_post(...)` stuurt dan geen POST opdrachten naar die Zendure
en gebruikt tijdelijk het laatst gemeten wattage uit de laatste succesvolle GET
response. Daardoor krijgt Home Assistant nog een response en krijgen gezonde
Zendures geen extra opdrachten die eigenlijk voor de tijdelijk onbereikbare
Zendure bedoeld waren.

De logregels `Queue cleanup: coalesced ... queued GET requests into 1 upstream
GET` en `Queue cleanup: deduplicated ... queued POST requests` betekenen dat de
proxy een opstopping bewust heeft samengevoegd. De metrics sensors
`sensor.zendure_proxy_queue_get_coalesced_total` en
`sensor.zendure_proxy_queue_post_deduplicated_total` tellen hoe vaak dat is
gebeurd.

### Testen vanuit HA Terminal

Gebruik vanuit de Home Assistant Terminal add-on de interne AppDaemon add-on hostnaam. Gebruik hier niet `127.0.0.1`, want `127.0.0.1` verwijst vanuit de Terminal add-on naar de Terminal container en niet naar de AppDaemon container.

Test de standaard report URL:

```bash
curl -i http://a0d7b954-appdaemon:8120/properties/report
```

Test de `/endpoint` report URL die Gielz meestal gebruikt:

```bash
curl -i http://a0d7b954-appdaemon:8120/endpoint/properties/report
```

Test de AppDaemon API report endpoint:

```bash
curl -i http://a0d7b954-appdaemon:5050/api/appdaemon/zendure_proxy_report
```

Test een POST request zonder echt vermogen te vragen:

```bash
curl -i \
  -X POST \
  -H "Content-Type: application/json" \
  -d '{"ping":"pong"}' \
  http://a0d7b954-appdaemon:8120/properties/write
```

Test dezelfde POST request via `/endpoint`:

```bash
curl -i \
  -X POST \
  -H "Content-Type: application/json" \
  -d '{"ping":"pong"}' \
  http://a0d7b954-appdaemon:8120/endpoint/properties/write
```

Een werkende GET geeft een HTTP response van de proxy terug. Een fout zoals `Failed to connect to 127.0.0.1 port 8120` betekent dat de test naar de verkeerde container wijst.

### Overstappen van Node-RED naar AppDaemon

Deze stappen zijn bedoeld voor gebruikers die de Node-RED proxy al hebben draaien en willen overstappen naar de AppDaemon/Python versie.

1. Maak eerst een backup van Home Assistant.

2. Noteer de IP-adressen van je Zendure devices uit het Node-RED blok `Vul hier de Zendure IP adressen in`.

3. Installeer AppDaemon en installeer daarna `Zendure zenSDK Proxy` via HACS zoals beschreven onder [AppDaemon via HACS](#appdaemon-via-hacs).

4. Open je AppDaemon `apps.yaml` en voeg de `zendure_proxy` configuratie toe uit [`examples/apps.yaml`](examples/apps.yaml).

5. Vul in `apps.yaml` dezelfde Zendure IP-adressen in die nu in Node-RED staan:

```yaml
ip_zendure_1: "192.168.x.x"
ip_zendure_2: "192.168.x.y"
ip_zendure_3: ""
```

6. Laat `proxy_ha_sensors_skip_existing: true` staan wanneer je de oude REST sensors al hebt. De AppDaemon proxy laat bestaande REST sensors dan met rust.

7. Herstart AppDaemon.

8. Controleer in de AppDaemon log of de proxy gestart is. Je zoekt naar regels zoals:

```text
Zendure proxy ... started
Device 1 SN:
Device 2 SN:
```

9. (optioneel) Test de AppDaemon proxy vanuit de Home Assistant Terminal add-on:

```bash
curl -i http://a0d7b954-appdaemon:8120/endpoint/properties/report
```

Een werkende test geeft `HTTP/1.1 200 OK` en een JSON response terug.

10. Pas in het Gielz dashboard het veld `Zendure 2400 AC IP-adres` aan van de Node-RED URL naar:

```text
a0d7b954-appdaemon:8120/endpoint
```

Gebruik niet `localhost:8120/endpoint` wanneer AppDaemon als Home Assistant add-on draait. `localhost` wijst vanuit Home Assistant Core niet naar de AppDaemon add-on.

11. (optioneel) Controleer in de AppDaemon log of Home Assistant de nieuwe proxy gebruikt. Je zoekt naar regels zoals:

```text
GET /endpoint/properties/report HTTP/1.1" 200
POST /endpoint/properties/write HTTP/1.1" 200
```

12. Test daarna een veilige stand in Gielz, bijvoorbeeld een lage laad- of ontlaadopdracht. Controleer of `sensor.vermogensopdracht_zendure_1`, `sensor.vermogensopdracht_zendure_2`, en eventueel `sensor.vermogensopdracht_zendure_3` blijven updaten.

13. Laat Node-RED nog even geïnstalleerd staan, maar zorg dat Gielz niet meer naar Node-RED wijst. Als de AppDaemon proxy stabiel werkt, kun je de Node-RED flow uitschakelen of verwijderen.

14. (optioneel) Verwijder de oude REST sensors alleen als je bewust wilt overstappen naar automatische proxy sensors. Laat de oude REST sensors staan wanneer je `unique_id` via YAML wilt behouden of wanneer je geen MQTT gebruikt.

Kort samengevat: eerst AppDaemon werkend maken, daarna pas het Gielz IP-adres wijzigen, en Node-RED pas uitzetten nadat je `GET ... 200` en `POST ... 200` in de AppDaemon log ziet.

### Release naar HACS

Voor testen als custom repository is een GitHub release niet verplicht. HACS leest zonder release de default branch.

Voor een nette gebruikerservaring maak je wel een GitHub release. HACS toont dan de laatste releases als updatekeuzes.

Controleer vóór een release dat de GitHub Action `Validate HACS` groen is.



## Bewust verschil met Node-RED bij drie Zendures

De AppDaemon/Python implementatie probeert backwards compatible te zijn met de
Node-RED implementatie. Er is één bewust verschil bij drie Zendures in Single
Mode.

Wanneer drie Zendures actief zijn in de proxy en de actieve Zendure door
SoC-balancering wisselt, stuurt de Node-RED 3-Zendure code het vermogen direct
naar de nieuw gekozen Zendure. Voorbeeld: de proxy krijgt een opdracht voor
`500 W` laden, Zendure 1 was actief, en de SoC waarden zijn `80%`, `70%`, en
`40%`. Node-RED kiest dan Zendure 3 als nieuwe actieve Zendure en kan de volle
opdracht direct naar Zendure 3 sturen.

De AppDaemon/Python implementatie kiest in die situatie bewust voor dezelfde
overgang die Node-RED al gebruikt bij twee Zendures. Gedurende
`singlemode_transition_timer`, standaard `40` seconden, blijven de oude en de
nieuwe actieve Zendure tijdelijk samen actief. De oude actieve Zendure krijgt
eerst ongeveer `95%` van het vermogen en de nieuwe actieve Zendure ongeveer
`5%`. Daarna schuift het vermogen stapsgewijs naar `75%/25%`, `50%/50%`, en
`25%/75%`. Na de timer krijgt de nieuwe actieve Zendure de opdracht alleen.

Deze afwijking is bewust. Een Zendure die in slaapstand of lage activiteit
stond, kan tijd nodig hebben om relais, modus, en vermogensregeling stabiel te
krijgen. Direct van `0 W` naar de volledige opdracht springen kan onrustiger
gedrag geven dan een korte overgang. De AppDaemon/Python implementatie gebruikt
daarom bij drie Zendures dezelfde zachte overgang als bij twee Zendures, ook al
slaat de Node-RED 3-Zendure code die overgang over.

