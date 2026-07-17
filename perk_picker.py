"""
perk_picker.py — OCR-based auto perk selection for The Tower.

Reads the "Choose a New Perk" dialog with Windows built-in OCR (winocr), ranks
the offered perk cards, taps the best one, and never picks blacklisted
trade-offs. Blacklist (per wiki https://the-tower-idle-tower-defense.fandom