# PocketAP

Jednoduchý Wi-Fi hotspot pro Windows v jednom souboru, ovládaný z terminálu.

Mobilní hotspot ve Windows se bez připojení k internetu často nezapne. PocketAP internet nepotřebuje, takže síť vytvoříš kdykoli. Vznikne místní síť pro propojení zařízení. Internet do ní aplikace sdílí jen tehdy, když to zapneš příkazem `internet zap`.

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
| `heslo NOVÉ_HESLO` | změní heslo (8 až 63 znaků bez diakritiky) |
| `internet zap\|vyp` | zapne nebo vypne sdílení internetu (výchozí je vypnuto) |
| `stop` | pozastaví hotspot |
| `start` | znovu spustí hotspot |
| `help` | zobrazí nápovědu |
| `exit` | vypne hotspot a ukončí aplikaci |

Změna názvu, hesla nebo sdílení internetu za běhu hotspot restartuje a připojená zařízení se musí připojit znovu. Změny zůstávají jen v paměti, po dalším spuštění platí zase výchozí hodnoty.

Aplikaci ukončíš také klávesami Ctrl+C nebo zavřením okna.

## Sdílení internetu

Příkaz `internet zap` sdílí do hotspotu aktuální připojení počítače k internetu přes službu Sdílení připojení k Internetu ve Windows.

- Sdílení potřebuje práva správce, aplikace si o ně při zapnutí řekne a otevře se v novém okně.
- Windows umí sdílet jen jedno připojení najednou, takže jiné zapnuté sdílení se tím vypne.
- Když se počítač přepojí na jinou síť, načti ji do hotspotu příkazy `stop` a `start`.
- Pokud sdílení selže, hotspot běží dál jako místní síť bez internetu.

## Když něco nejde

- Zkontroluj, že je zapnutá Wi-Fi a vypnutý režim Letadlo.
- Vypni Mobilní hotspot ve Windows a Miracast, protože používají stejný Wi-Fi Direct.
- Pokud adresy klientům nepřidělí Windows, aplikace si vyžádá práva správce a spustí vlastní DHCP server.
