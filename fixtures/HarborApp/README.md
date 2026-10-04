# HarborApp

HarborApp is the controlled macOS archive and export fixture for BuildHarbor 0.2. The shared `HarborApp` scheme archives a minimal SwiftUI application without launching it.

Release uses manual ad-hoc signing with identity `-`, an empty development team, and no provisioning profile, entitlements, capabilities, scripts, or plugins. Ad-hoc signing does not select a private keychain identity and is only for local validation. It does not represent Developer ID, notarization, App Store, or other distribution signing.

`ExportOptions.plist` requests Xcode's macOS Copy App export with only `method = mac-application` and `destination = export`. It deliberately contains no team, certificate, profile, installer, or upload settings. If Xcode requests an account or private signing identity, validation must stop rather than add credentials to this fixture.

BuildHarbor must supply the archive path, export path, result bundle, DerivedData, caches, and temporary directories beneath the verified external storage root. The fixture contains no output paths and must not be run directly until the 0.2 planner manages those locations.
