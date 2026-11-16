Regenerates app/static/theme.css (Material 3 color schemes), fonts/ and icons.js.
Add or change seed colors in `seeds` inside gen.mjs, then:

    npm i @material/material-color-utilities @fontsource/roboto @material-symbols/svg-400 esbuild
    npx esbuild gen.mjs --bundle --platform=node --format=esm --outfile=gen.bundle.mjs && node gen.bundle.mjs
