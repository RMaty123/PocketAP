# PocketAP

Jednoduchý Wi-Fi hotspot pro Windows v jednom souboru, ovládaný z terminálu.

Mobilní hotspot ve Windows se bez připojení k internetu často nezapne. PocketAP internet nepotřebuje, takže síť vytvoříš kdykoli. Vznikne místní síť pro propojení zařízení, internet do ní aplikace nesdílí.

## Požadavky

- Windows 11 a Wi-Fi adaptér s podporou Wi-Fi Direct
- Python 3.11 nebo novější
- Internet jen při prvním spuštění, kdy se automaticky nainstalují potřebné balíčky

## Spuštění

```
python pocketap.py
```

Hotspot se hned zapne jako síť `PocketAP` s heslem `123456789`. Název a heslo můžeš zadat i při spuštění:

```
python pocketap.py MojeSit -p MojeHeslo123
```

Výchozí heslo je snadné uhodnout, proto si pro delší používání nastav vlastní.

## Příkazy

| Příkaz | Co udělá |
| --- | --- |
| `status` | zobrazí stav hotspotu |
| `ssid NÁZEV` | změní název sítě |
| `heslo HESLO` | změní heslo (8 až 63 znaků bez diakritiky) |
| `stop` | pozastaví hotspot |
| `start` | znovu spustí hotspot |
| `help` | zobrazí nápovědu |
| `exit` | vypne hotspot a ukončí aplikaci |

Změna názvu nebo hesla za běhu hotspot restartuje a připojená zařízení se musí připojit znovu. Změny zůstávají jen v paměti, po dalším spuštění platí zase výchozí hodnoty.

Aplikaci ukončíš také klávesami Ctrl+C nebo zavřením okna.

## Když něco nejde

- Zkontroluj, že je zapnutá Wi-Fi a vypnutý režim Letadlo.
- Vypni Mobilní hotspot ve Windows a Miracast, protože používají stejný Wi-Fi Direct.
- Pokud adresy klientům nepřidělí Windows, aplikace si vyžádá práva správce a spustí vlastní DHCP server.
