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

## 📦 Automated publishing

This package is generated from [`sabaatworld/ha-core`](https://github.com/sabaatworld/ha-core) at source revision `a80afe71e45bbc4a32cde036a022adc32d1217f0` and published as `2026.7.2-v0.0.7`. HACS uses the GitHub Release for each published version to offer updates.
## ✨ Why LIFX Ultimate?

I love LIFX lights. They are comparatively affordable, and I find their
colours brighter and more vibrant than Philips Hue. Home Assistant is how I
control every LIFX light in my home, but two frustrations always kept getting
in the way:

1. My lights are arranged into groups, such as Tree Lamps, and often controlled
   by one remote. It was frustrating when lights in the same group did not turn
   on, turn off, or transition at the same time.
2. LIFX supported transition durations for direct on and off operations, but
   not convenient default fades for general state changes. That meant adding a
   transition to every automation—and some integrations offered no way to do
   that.

LIFX Ultimate is the solution: reliable, synchronised Device Groups and
configurable fade defaults for the way Home Assistant is actually used.

## 🎯 Device Groups

A Device Group is one virtual LIFX light backed by selected existing LIFX
light entities. Use it for lights that should act as one—for example, several
Tree Lamps controlled together.

To create one, go to **Settings → Devices & services → Add integration → LIFX
Ultimate → Add Device Group**, give it a name, and select the member light
entities. The members remain available as individual lights too.

When you control a Device Group, LIFX Ultimate prepares the per-light commands
first, then releases them together against one shared monotonic deadline. This
keeps the network command-send timing aligned at millisecond scale, including
dependent multi-command operations that wait for acknowledgements before the
next synchronised stage. The exact moment each bulb visibly changes can still
vary with its firmware, Wi-Fi, and internal rendering.

> [!WARNING]
> Turning a Device Group off uses **virtual off**: its members are set to zero
> brightness instead of having their power cut. They remain powered, consume
> marginally more energy than normal standby, and appear as on at zero
> brightness in the LIFX app. Home Assistant presents the group and individual
> lights as off.

## 🌈 Fade and transition defaults

Every physical LIFX light and Device Group has three configurable Number
entities, in seconds:

- **Fade On Time** — used when the light turns on.
- **Fade Off Time** — used when the light turns off.
- **Cross Fade Time** — used when an already-on light changes colour or
  brightness without turning on or off.

Set a non-zero value to use a default fade. A value of `0` means “do not
override”; it falls through to the next applicable default, or produces an
immediate change when none is configured.

## 📐 Transition priority

The duration for each command is chosen in this order:

1. A `transition` supplied in the current service call always wins, including
   an explicit `0`.
2. For a Device Group, its matching non-zero Fade On, Fade Off, or Cross Fade
   setting wins.
3. If that group setting is `0`, each member uses its matching physical-light
   setting. Members can therefore have different durations unless you set a
   group override.
4. For a physical LIFX light, its matching non-zero setting is used.
5. If no setting supplies a duration, the change is immediate.

This lets an automation choose a one-off transition when needed, while your
light and group defaults handle the rest.
