# LIFX Ultimate

LIFX Ultimate is a custom Home Assistant integration that replaces the built-in
LIFX integration with the latest LIFX Ultimate enhancements.

## Installation

1. In HACS, open **Integrations** and select **Custom repositories**.
2. Add `https://github.com/sabaatworld/ha-lifx-ultimate` as an **Integration**.
3. Install **LIFX Ultimate** and restart Home Assistant.

It intentionally keeps the technical domain `lifx`, so existing LIFX config
entries are retained and this package overrides Home Assistant's built-in LIFX
integration.

## Updates

This repository is generated automatically from

[`sabaatworld/ha-core`](https://github.com/sabaatworld/ha-core) whenever its
LIFX source changes. HACS tracks normal commits; no manual release selection is
required.
Generated from source revision `initial-bootstrap` as version `0.0.1`.
