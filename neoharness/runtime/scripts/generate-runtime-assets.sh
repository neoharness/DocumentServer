#!/bin/sh
set -eu

root="${1:-/opt/neoharness-office/documentserver}"
bin="${root}/server/FileConverter/bin"
tools="${root}/server/tools"

test -x "${bin}/docbuilder"
test -x "${bin}/x2t"
test -x "${tools}/allfontsgen"
test -x "${tools}/allthemesgen"
test -d "${root}/core-fonts"
test -d "${root}/sdkjs"

export LD_LIBRARY_PATH="${bin}${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"

mkdir -p \
    "${root}/fonts" \
    "${root}/sdkjs/common/Images" \
    "${bin}"

rm -f \
    "${root}/sdkjs/common/AllFonts.js" \
    "${bin}/AllFonts.js" \
    "${bin}/font_selection.bin"

# The source-controlled core-fonts tree is the complete and deterministic font
# input for both viewer and headless builds. Host/user fonts are intentionally
# excluded so two builds cannot paginate the same file differently.
"${tools}/allfontsgen" \
    --input="${root}/core-fonts" \
    --allfonts-web="${root}/sdkjs/common/AllFonts.js" \
    --allfonts="${bin}/AllFonts.js" \
    --images="${root}/sdkjs/common/Images" \
    --selection="${bin}/font_selection.bin" \
    --output-web="${root}/fonts" \
    --use-system="false" \
    --use-system-user-fonts="false"

for variant in desktop ios android; do
    case "${variant}" in
        desktop)
            "${tools}/allthemesgen" \
                --converter-dir="${bin}" \
                --src="${root}/sdkjs/slide/themes" \
                --output="${root}/sdkjs/common/Images"
            ;;
        ios)
            "${tools}/allthemesgen" \
                --converter-dir="${bin}" \
                --src="${root}/sdkjs/slide/themes" \
                --output="${root}/sdkjs/common/Images" \
                --postfix="ios" \
                --params="280,224"
            ;;
        android)
            "${tools}/allthemesgen" \
                --converter-dir="${bin}" \
                --src="${root}/sdkjs/slide/themes" \
                --output="${root}/sdkjs/common/Images" \
                --postfix="android" \
                --params="280,224"
            ;;
    esac
done

# CreateFile expects its stock empty templates beneath FileConverter/bin.
rm -rf "${bin}/empty"
ln -s "../../../document-templates/new/default" "${bin}/empty"

(cd "${bin}" && ./x2t -create-js-cache)

test -s "${root}/sdkjs/common/AllFonts.js"
test -s "${bin}/AllFonts.js"
test -s "${bin}/font_selection.bin"
test -s "${bin}/cmap.bin"
