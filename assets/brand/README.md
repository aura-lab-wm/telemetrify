# telemetrify · brand assets

| File | Use |
| --- | --- |
| `mark.svg` | Primary orange logo mark (no background). Drop into any dark surface. |
| `mark-mono.svg` | `currentColor` version. Inherits ink color from the surrounding context. |
| `favicon.svg` | Mark on a rounded dark tile. Linked from the UI as `<link rel="icon">`. |
| `favicon.ico` | Multi-size ICO (16/32/256) for legacy browsers. |
| `avatar.svg` / `avatar-512.png` / `avatar-1024.png` | Square avatars for GitHub repo / org. Upload `avatar-1024.png` in GitHub → repo settings → social preview / icon. |
| `og-image.svg` / `og-image.png` | 1200×630 social card. |
| `MenubarIconTemplate.png` / `@2x` / `@3x` | macOS menu-bar template icons (22pt). Name ends in `Template` so AppKit recolors automatically. |
| `AppIcon-256.png` | General-purpose 256² PNG of the favicon tile. |

## Regenerating

```sh
cd assets/brand
rsvg-convert -w 22  -h 22  -f png mark-mono.svg > MenubarIconTemplate.png
rsvg-convert -w 44  -h 44  -f png mark-mono.svg > MenubarIconTemplate@2x.png
rsvg-convert -w 66  -h 66  -f png mark-mono.svg > MenubarIconTemplate@3x.png
rsvg-convert -w 1200 -h 630 -f png og-image.svg  > og-image.png
rsvg-convert -w 1024 -h 1024 -f png avatar.svg   > avatar-1024.png
magick favicon.svg -resize 16x16 favicon-16.png
magick favicon.svg -resize 32x32 favicon-32.png
magick favicon-16.png favicon-32.png AppIcon-256.png favicon.ico
```

## Color tokens

- Primary accent: `#ff5c00` (`--phosphor`)
- Background tile: `#0a0a0a` (`--panel`)
- Ink: `#fafafa` (`--ink`)
