#!/usr/bin/env bash
set -euo pipefail
fixture_dir="$(cd "$(dirname "$0")" && pwd)"
repo_root="$(cd "$fixture_dir/../.." && pwd)"
derived_data="$repo_root/DerivedData/VisualIterationFixture"
project="$fixture_dir/VisualIterationFixture.xcodeproj"
project_json="$(mktemp /tmp/hermes-ios-fixture-project.XXXXXX.json)"
trap '/bin/rm -f "$project_json"' EXIT

plutil -convert json -o "$project_json" "$project/project.pbxproj"
PYTHONDONTWRITEBYTECODE=1 python3 "$fixture_dir/validate_project.py" "$project_json" "VisualIterationFixture/VisualIterationFixtureApp.swift"
plutil -lint "$fixture_dir/VisualIterationFixture/Info.plist"
xmllint --noout "$project/xcshareddata/xcschemes/VisualIterationFixture.xcscheme"

settings="$(xcodebuild \
  -project "$project" \
  -target VisualIterationFixture \
  -configuration Debug \
  -sdk iphonesimulator \
  -showBuildSettings \
  SYMROOT="$derived_data/ProjectPreflight/Products" \
  OBJROOT="$derived_data/ProjectPreflight/Intermediates")"
for required_setting in \
  'TARGET_NAME = VisualIterationFixture' \
  'PRODUCT_TYPE = com.apple.product-type.application' \
  'PRODUCT_BUNDLE_IDENTIFIER = dev.hermes.VisualIterationFixture' \
  'PLATFORM_NAME = iphonesimulator' \
  'IPHONEOS_DEPLOYMENT_TARGET = 17.0'; do
  if ! /usr/bin/grep -Fq "$required_setting" <<<"$settings"; then
    echo "Missing resolved Xcode build setting: $required_setting" >&2
    exit 1
  fi
done

if xcrun simctl list runtimes available 2>/dev/null | /usr/bin/grep -q 'iOS'; then
  xcodebuild \
    -project "$project" \
    -scheme VisualIterationFixture \
    -configuration Debug \
    -sdk iphonesimulator \
    -destination 'generic/platform=iOS Simulator' \
    -derivedDataPath "$derived_data" \
    CODE_SIGNING_ALLOWED=NO \
    build
  exit 0
fi

# CI/developer Macs may have the Simulator SDK but no bootable runtime component.
# Compile the same app target directly for the Simulator ABI so U1 remains build-verifiable.
sdk_path="$(xcrun --sdk iphonesimulator --show-sdk-path)"
app_dir="$derived_data/Build/Products/Debug-iphonesimulator/VisualIterationFixture.app"
rm -rf "$app_dir"
mkdir -p "$app_dir"
cp "$fixture_dir/VisualIterationFixture/Info.plist" "$app_dir/Info.plist"
xcrun --sdk iphonesimulator swiftc \
  -target "$(uname -m)-apple-ios17.0-simulator" \
  -sdk "$sdk_path" \
  -parse-as-library \
  -emit-executable \
  "$fixture_dir/VisualIterationFixture/VisualIterationFixtureApp.swift" \
  -o "$app_dir/VisualIterationFixture"
/usr/bin/file "$app_dir/VisualIterationFixture"
echo "Built Simulator app bundle with SDK fallback (no bootable iOS runtime is installed): $app_dir"
