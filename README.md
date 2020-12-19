Extract files from apple devices, such as one made by libimobiledevices / idevicebackup2

(it may work with itunes backups as well, but I do not have it, so I cannot test)

This mainly involves reading manifest.db and copying files with appropriate name

Programmed defensively to fail-fast if any information is missing.
More checks we can add:
- Backup dir has no files not accounted for
- Parse info.plist / status.plist
