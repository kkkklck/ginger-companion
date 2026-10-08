# User guide · Ginger Companion

[← Back to the project](../README.md)

## Launch and shutdown

Use Python 3.10 or later. From the project directory, run:

```bash
python ginger_app.py
```

On Windows, the included `启动姜小伴.vbs` launcher opens the application without a console window. It looks for supported local Python installations; on another machine, ensure Python is available or use the command above.

The browser normally opens `http://127.0.0.1:8765`. The server is restricted to local access. Closing the page may leave the server running; use the shutdown action in settings and help to exit completely.

## First use

1. Explore the demonstration field without an API key.
2. Add a plant-condition or fieldwork entry and inspect the journal.
3. Replace the demonstration field when ready. The confirmation clears demonstration records and conversations.
4. Set location, area, planting date, and harvest purpose. Historical yield, variety, and local growing-cycle length are optional.

The current interface is Chinese. The demo, add-record, field replacement, settings, and shutdown actions are described here by their function.

## Location and weather

Search for a location and confirm the result. If a detailed village address is unavailable, try a nearby administrative area, enter WGS84 coordinates, or configure an optional Amap Web Service key.

The application attempts weather updates when started and periodically while open. Failed requests may show timestamped cached forecasts. Weather-model soil values are regional estimates rather than sensor readings from your field.

## AI setup

In settings, enter the API key, compatible Base URL, and a text-model ID available to your account. For Alibaba Cloud Model Studio, match the endpoint region to the key. Test the connection before chatting. Start with the automatic thinking setting unless your model supports a specific option.

API keys stay in memory for the current run. Endpoint, model, and thinking preferences are saved. Exiting clears the keys.

AI requests include relevant growing information, recent records, conversation context, weather and estimate information, and retrieved paper excerpts. Notes you enter may contain personal information and travel with the request. Calls may incur provider charges.

## Soil tests, yield samples, and harvests

Soil entries can retain measurements, units, and method notes. Different analytical methods and sample types may not be directly comparable.

Yield samples record area and fresh weight and distinguish growing-season samples from harvest samples. Harvest samples taken on the same date are combined using total weight divided by total sampled area. Growing-season samples are stored without directly extrapolating final yield.

Harvest-window and yield ranges are prototype reference estimates. They have not been calibrated into a validated local prediction model. Fertilizer and treatment decisions require local guidance and product-label checks.

## Accessibility and backups

Larger text is available in the top bar. Speech recognition depends on browser support and permissions; read-aloud depends on installed voices. Typing remains available.

Field records and conversations are stored in `ginger_companion_data`. Export a backup through settings. For a complete backup, stop the application and copy that entire directory. API keys are excluded from exports.

## Optional paper capabilities

The public application can start with an empty paper library. To use paper evidence, connect local materials as described in the [Paper library guide](PAPER_LIBRARY.md). Some original-page-image provenance checks require the optional `pymupdf` package.
