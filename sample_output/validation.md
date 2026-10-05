# Validation on dvr-pramanshala 0.4.0

| Image | Mode | Recovered | Recovery rate | False positives | Timed | Temporal accuracy (±1 s) | Camera accuracy (attributed) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dahua_intact.img | native | 5400/5400 | 100.00% | 0 | 5400 | 100.00% | 100.00% (5400) |
| dahua_no_overlay.img | native | 1800/1800 | 100.00% | 0 | 1800 | 100.00% | 100.00% (1800) |
| dahua_no_overlay_control.img | native | 1800/1800 | 100.00% | 0 | 1800 | 100.00% | 100.00% (1800) |
| dahua_overwritten.img | native | 5244/5244 | 100.00% | 0 | 5244 | 100.00% | 100.00% (5244) |
| hikvision_formatted.img | native | 5400/5400 | 100.00% | 0 | 5400 | 100.00% | 100.00% (5400) |
| hikvision_intact.img | native | 5400/5400 | 100.00% | 0 | 5400 | 100.00% | 100.00% (5400) |
| hikvision_overwritten.img | native | 5401/5401 | 100.00% | 0 | 5390 | 100.00% | 100.00% (5390) |
| dahua_intact.img | universal | 5400/5400 | 100.00% | 0 | 4540 | 99.78% | 99.81% (5400) |
| dahua_no_overlay.img | universal | 1800/1800 | 100.00% | 0 | 0 | n/a | 100.00% (1800) |
| dahua_no_overlay_control.img | universal | 1800/1800 | 100.00% | 0 | 0 | n/a | n/a (0) |
| dahua_overwritten.img | universal | 5244/5244 | 100.00% | 1 | 4884 | 99.61% | 99.64% (5244) |
| hikvision_formatted.img | universal | 5400/5400 | 100.00% | 0 | 4330 | 99.77% | 99.81% (5360) |
| hikvision_intact.img | universal | 5400/5400 | 100.00% | 0 | 4330 | 99.77% | 99.81% (5360) |
| hikvision_overwritten.img | universal | 5401/5401 | 100.00% | 0 | 4371 | 98.60% | 98.90% (5341) |
