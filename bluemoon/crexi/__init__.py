"""Crexi bulk batches: export a CSV of properties for Crexi, import Crexi's reply, keep the history.

Modules (import them directly; this file imports nothing so the credit code can use `period` freely):
- period   credit periods that reset on config.yaml crexi.reset_day
- merge    the keep-the-original / alternates rule for incoming data (also used by the Crexi property import)
- batches  pick the next N properties, write the export file, close or cancel a batch, history
- reply    read, match and apply Crexi's reply file (preview first)
- cli      `uv run run.py crexi ...`

The app never touches Crexi: files only, moved by hand.
"""
