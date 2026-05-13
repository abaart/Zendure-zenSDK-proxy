# Zendure zenSDK Proxy Add-on

This add-on runs the Python Zendure proxy inside Home Assistant.

Configure the Zendure device IP addresses in the add-on options. Then enter
`localhost:1880` in the Gielz Zendure IP field when the Gielz integration runs
inside the same Home Assistant host. Use `<home-assistant-ip>:1880` when another
system calls the proxy.

The proxy keeps the same main endpoints as the Node-RED version:

- `GET /properties/report`
- `POST /properties/write`
- `GET /metrics`
- `GET /healthz`

Short Zendure read failures are hidden for up to 60 seconds by reusing the last
valid read response for that device. Write failures are returned immediately.

