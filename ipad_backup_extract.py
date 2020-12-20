#!/usr/bin/python3 -B

# Licensed under GPL v3

import sqlite3
import argparse
import glob
import sys
import os
import sqlite3
import biplist # sudo apt install python3-biplist
import pprint
import stat
import shutil
import datetime

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('input_dir',
                        help='Backup input dir (treated as read-only)')
    parser.add_argument('-l', '--list', action='store_true',
                        help='List files to stdout')
    parser.add_argument('-x', '--extract', metavar='DEST',
                        help='Extract (copy) files to DEST')
    parser.add_argument('-s', '--size-check', action='store_true',
                        help='check the size of file in db vs on disk and warn if mismatch')

    parser.add_argument('-c', '--camera', action='store_true',
                        help='Limit extract/list to camera images and videos only')
    args = parser.parse_args()

    norm_input = os.path.realpath(args.input_dir)
    manifest_name = os.path.join(norm_input, 'Manifest.db')
    open(manifest_name, 'rb').close()

    # sqlite3 claims to su
    
    db = sqlite3.connect('file://' + manifest_name + '?mode=ro&immutable', uri=True)

    # I am getting two tables ("files" and "properties") and the latter is always empty
    count = 0
    # same objects
    same_skip = 0

    if args.extract:
        print('Extracting to', args.extract, file=sys.stderr, flush=True)
    
    for row in db.cursor().execute('SELECT fileID, domain, relativePath, flags, file FROM Files'):
        fileID, domain, relativePath, flags, filePlist = row
        assert '/' not in domain, repr(row)

        if args.camera:
            if domain != 'CameraRollDomain':
                continue
            if relativePath.startswith('Media/'):
                # Strip "/Media" prefix
                dom_path = relativePath.split('/', 1)[1]
            else:
                # Something else, include to prevent data loss
                dom_path = '__Other__/' + relativePath
                
            if '/PhotoData/' in dom_path or '/MediaAnalysis/' in dom_path:
                # Skip thumbnails or analytics
                continue
        else:
            dom_path = os.path.join(domain, relativePath)
                
        rowinfo = dict(fileId=fileID, domain=domain, relativePath=relativePath, flags=flags)
        
        # parse plist for things like attributes. Hardcode the only known format, so we know if
        # something strange comes up.
        plist = biplist.readPlistFromString(filePlist)
        pl_objects = plist.pop('$objects')
        assert plist == {'$archiver': 'NSKeyedArchiver',
                         '$top': {'root': biplist.Uid(1)},
                         '$version': 100000}, \
                         'unexpected plist toplevel\n' + pprint.pformat(dict(row=rowinfo, plist=plist, pl_objects=pl_objects))

        # pl_objects has [0] $null, [1] pl_attrib, rest as described in pl_attrib
        assert (len(pl_objects) >= 4 and pl_objects[0] == '$null' and isinstance(pl_objects[1], dict)), \
                'invalid pl_objects toplevel struct\n' + pprint.pformat(dict(row=rowinfo, pl_objects=pl_objects))    
        pl_attrib = pl_objects[1]

        # Decode "Uid" values in pl_attrib which actually point to pl_objects elements
        indexed = {i + 2: val for i, val in enumerate(pl_objects[2:])}
        for k in ['RelativePath', 'ExtendedAttributes', '$class', 'Target', 'Digest']:
            if isinstance(pl_attrib.get(k, None), biplist.Uid):
                val = pl_attrib[k] = indexed.pop(pl_attrib[k].integer)
                if isinstance(val, dict) and isinstance(val.get('$class', None), biplist.Uid):
                    val['$class'] = indexed.pop(val['$class'].integer)
                    
        assert not indexed, 'Leftovers in indexed, Uid missing in pl_attrib?\n' + pprint.pformat(
            dict(row=rowinfo, pl_attrib=pl_attrib, indexed=indexed))

        # Match the database row info, prevent path escapes (security)
        assert pl_attrib['RelativePath'] == relativePath and '..' not in dom_path and not os.path.isabs(dom_path), \
            'Invalid relativepath\n' + pprint.pformat(                
                dict(row=rowinfo, pl_attrib=pl_attrib))
        
        pl_attrib.pop('RelativePath')
        # Make sure "class" is reasonable
        assert pl_attrib['$class'] == {'$classes': ['MBFile', 'NSObject'], '$classname': 'MBFile'}, \
            'Weird class\n' + pprint.pformat(
                dict(row=rowinfo, pl_attrib=pl_attrib))
        pl_attrib.pop('$class')

        COMMON_NAMES = {
            'Birth', 'Flags', 'GroupID', 'InodeNumber', 'LastModified', 'LastStatusChange', 'Mode',
            'ProtectionClass', 'Size', 'UserID'
        }
        common_missing = COMMON_NAMES.difference(pl_attrib.keys())
        uncommon_keys = set(pl_attrib.keys()).difference(COMMON_NAMES)
        assert not common_missing, 'common keys missing\n' + pprint.pformat(
            dict(row=rowinfo, pl_attrib=pl_attrib, common_missing=common_missing, uncommon_keys=uncommon_keys))
        

        # Decode extended attributes
        if 'ExtendedAttributes' in pl_attrib:
            exattr = pl_attrib['ExtendedAttributes']
            assert (sorted(exattr.keys()) == ['$class', 'NS.data'] and
                    exattr['$class'] == {
                        '$classname': 'NSMutableData',
                        '$classes': ['NSMutableData', 'NSData', 'NSObject']}), \
                        'extattr meta malformed\n' + pprint.pformat(
                            dict(row=rowinfo, pl_attrib=pl_attrib))
            exattr = biplist.readPlistFromString(exattr['NS.data'])
            for k, v in exattr.items():
                if k in ['com.apple.assetsd.addedDate', 'com.apple.assetsd.customCreationDate']:
                    v = biplist.readPlistFromString(v)
                    if isinstance(v, datetime.datetime):
                        v = v.isoformat()
                    exattr[k] = v
                elif isinstance(v, bytes) and len(v) == 2:
                    exattr[k] = v[0] + v[1] * 256
            pl_attrib['ExtendedAttributes'] = exattr                                    
        
        mode = pl_attrib['Mode']
            
        # must be regular file or symlink
        assert (flags in [1, 2] and uncommon_keys.issubset({'ExtendedAttributes', 'Digest'})) or (
            flags == 4 and uncommon_keys == {'Target'} and stat.S_ISLNK(mode)), \
                'Unexpected object type\n' + pprint.pformat(
                    dict(row=rowinfo, pl_attrib=pl_attrib, uncommon_keys=uncommon_keys))
        
        if flags == 1:
            # Unknown what this means
            pl_attrib['db_flags'] = flags

        if args.list:
            other = dict(**pl_attrib)
            if other['Flags'] == 0:
                del other['Flags']
            if other['ProtectionClass'] == 0:
                del other['ProtectionClass']
            mode = other.pop('Mode')
            print('%s %s %s mode=%5o own=%d:%d size=%d mtime=%d ctime=%d btime=%d ino=0x%X' % (
                fileID, dom_path,
                ('REG' if stat.S_ISREG(mode) else 'DIR' if stat.S_ISDIR(mode) else 'OTHER'),
                mode, other.pop('UserID'), other.pop('GroupID'), other.pop('Size'),
                other.pop('LastModified'), other.pop('LastStatusChange'), other.pop('Birth'),
                other.pop('InodeNumber')),
                other, flush=True)

        src_name = None
        if stat.S_ISREG(mode) and (args.extract or args.size_check):
            src_name = os.path.join(norm_input, fileID[:2], fileID)
            src_size = os.stat(src_name).st_size
            #assert src_size == pl_attrib['Size'], 'file contents are bad\n' + pprint.pformat(
            #        dict(row=rowinfo, pl_attrib=pl_attrib, uncommon_keys=uncommon_keys,
            #             src_name=src_name, src_size=src_size))
            if src_size != pl_attrib['Size'] and args.size_check:
                print('* warning: size mismatch (db %d, fs %d) for %s (%s)' % (
                    pl_attrib['Size'], src_size, dom_path, fileID), file=sys.stderr)
        
        if args.extract:
            dest = os.path.join(args.extract, dom_path)
            mtime = pl_attrib['LastModified']
            if stat.S_ISDIR(mode):
                try:
                    existing = os.stat(dest)
                except FileNotFoundError:
                    existing = None

                if existing and stat.S_ISDIR(existing.st_mode) and int(existing.st_mtime) == mtime:
                    same_skip += 1
                else:
                    os.makedirs(dest, exist_ok=True)
                    os.utime(dest, (mtime, mtime))
                    # Do not mess with dir's permissions
                    
            elif stat.S_ISREG(mode):
                # does this exist already by any chance?
                try:
                    existing = os.stat(dest)
                except FileNotFoundError:
                    existing = None

                if existing and (stat.S_ISREG(existing.st_mode) and int(existing.st_mtime) == mtime and
                                 (existing.st_mode & 0o0777) == (mode & 0o0777)):
                    # assume same content
                    # TODO(check checksum  here)
                    same_skip += 1
                else:
                    if not existing:
                        os.makedirs(os.path.dirname(dest), exist_ok=True)
                    shutil.copy(src_name, dest)  # safe because we do not do symlinks
                    os.utime(dest, (mtime, mtime))
                    os.chmod(dest, mode)
                    
            elif stat.S_ISLNK(mode):
                print('* skipping symlink %r -> %r' % (dest, pl_attrib['Target']), file=sys.stderr)
            else:
                print('* skipping unknown obj %r (%o)' % (dest, mode), file=sys.stderr)

            if (count % 5000) == 0:
                print('* %d files extracted so far' % count, file=sys.stderr, flush=True)
                
        count += 1

    if same_skip:
        print('* skipped %d files which were already there' % (same_skip, ), file=sys.stderr)
    print('* done, %d objects' % (count, ), file=sys.stderr)
                
    
if __name__ == '__main__':
    main()
