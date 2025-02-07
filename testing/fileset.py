# Copyright (C) 2021 Eric Lavarde <ewl+rdiffbackup@lavar.de>
#
# This program is licensed under the GNU General Public License (GPL).
# you can redistribute it and/or modify it under the terms of the GNU
# General Public License as published by the Free Software Foundation,
# Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA;
# either version 2 of the License, or (at your option) any later version.
# Distributions of rdiff-backup should include a copy of the GPL in a
# file called COPYING.  The GPL is also available online at
# https://www.gnu.org/copyleft/gpl.html.
"""
This library allows to create a set of files (and directories), using a
structure of dictionaries.

The name of the files and directories in the base directory are used as keys
in a dictionary of dictionaries.

A directory has the type "dir/directory" or a "contents" key to contain sub-directories or files.
A directory can also have the "rec" key, which is a dictionary of keys to apply by default to itself and all its children.

Both directories and files can have the following keys:
* a "mode" to define the chmod to apply, differentiated as "dmode" resp. "fmode"

Files can also have the following keys:
* "content" which is written to the file.
* "open" flag, "t" or "b" for the write mode to the file
* "inode" to hardlink files together, the value is not important, it must just be unique within the fileset (it might not even be an integer).

Then there are symlinks with type "link", and the "target" parameter, applied as-is.

Example:
{
    "a_dir": {
        "rec": {"fmode": 0o444, "content": "default content"},
        "contents": {"fileA": {"content": "initial"}, "fileB": {}}
    },
    "empty_dir": {"type": "dir", "dmode": 0o777},
    "a_bin_file": {"content": b"some_binary_content", "open": "b"},
}

NOTE: the format is meant to be as similar as possible to `tree -J`, so that its output could be eventually used to re-create a file structure.

TODO: fully support tree format with a list of dictionaries, instead of a dictionary of dictionaries, where the key is the filename.
"""

import os
import shutil
import stat


def create_fileset(base_dir, structure, recurse={}):
    """
    Create the file set as represented by the structure in the base directory

    base_dir can be path-like, str or bytes,
    structure contains names as str pointing to further structures
    recurse can be used to set settings in the whole hierarchy
    """
    # we create a new inodes dictionary for each fileset
    recurse["inodes"] = {}
    _create_directory(SetPath(base_dir, {"type": "directory"}, recurse))
    for name in structure:
        _create_fileset(os.path.join(os.fsdecode(base_dir), name),
                        structure[name], recurse)


def remove_fileset(base_dir, structure):
    """
    Remove the file set as represented by the strucutre in the base directory

    The base directory itself isn't removed unless it's empty
    """
    for name in structure:
        fullname = os.path.join(os.fsdecode(base_dir), name)
        struct = structure[name]
        set_path = SetPath(fullname, struct)
        try:
            if set_path.get_type() == "directory":
                _rmtree(fullname)
            else:
                os.remove(fullname)
        except FileNotFoundError:
            pass  # if the file doesn't exist, we don't need to remove it
        except IsADirectoryError:
            _rmtree(fullname)
        except NotADirectoryError:
            os.remove(fullname)

    # at the end we try to remove the base directory, if it's empty
    try:
        os.removedirs(base_dir)
    except OSError:
        pass  # directory isn't empty, we don't really care


def compare_paths(path1, path2):
    """
    Compare two paths, possibly created by this library

    Comparaison is made recursively according to the names of the files and
    sub-directories.
    If there are commonalities, those common files/dirs are then compared for
    type, link numbers, size and/or content, access mode, uid, gid.

    The result is a list of explained differences, empty if there are no
    differences. None is returned if the two paths happen to point at the
    same directory.
    """
    differences = []
    # if the paths are pointing to the same file, no need to compare
    if os.path.samefile(path1, path2):
        return None

    # compare first the paths as normal files
    stat1 = os.lstat(path1)
    stat2 = os.lstat(path2)

    differences += _compare_files(path1, stat1, path2, stat2)

    # stop the comparaison here if the paths don't both point to directories
    if not (stat.S_ISDIR(stat1.st_mode) and stat.S_ISDIR(stat2.st_mode)):
        return differences

    # then compare them as directories
    files1 = set(os.listdir(os.fsdecode(path1)))
    files2 = set(os.listdir(os.fsdecode(path2)))
    if len(files1 - files2):
        differences.append(
            "Files {fi} are in base dir {bd1} but not in {bd2}".format(
                fi=files1 - files2, bd1=path1, bd2=path2))
    if len(files2 - files1):
        differences.append(
            "Files {fi} are not in base dir {bd1} but in {bd2}".format(
                fi=files2 - files1, bd1=path1, bd2=path2))
    if files1.isdisjoint(files2):
        return differences  # there are no files in common

    for file in files1 & files2:
        next_path1 = os.path.join(os.fsdecode(path1), file)
        next_path2 = os.path.join(os.fsdecode(path2), file)
        differences += compare_paths(next_path1, next_path2)

    return differences


class SetPath():
    """
    Holds a path's own settings and the recursive ones, so that they can be
    transparently combined
    """
    defaults = {
        "dmode": 0o755,
        "fmode": 0o644,
        "open": "t",
        "content": "",
    }
    type_synonyms = {
        "dir": "directory",
        "symlink": "link",
        "hardlink": "file",
    }
    path = ""
    path_type = ""
    values = {}
    recurse = {}

    @classmethod
    def get_canonic_type(cls, path_type):
        return cls.type_synonyms.get(path_type, path_type)

    def __init__(self, path, values={}, recurse={}, new_rec={}):
        """
        Initiate the settings based on own values, recursive ones, and
        additional new ones, to be combined into one recursive set of settings
        """
        self.path = path
        path_type = values.get("type")
        if path_type:
            self.path_type = self.get_canonic_type(path_type)
        elif "contents" in values:
            self.path_type = "directory"
        elif "target" in values:
            self.path_type = "link"
        else:
            self.path_type = "file"
        self.values = values
        self.recurse = recurse.copy()
        self.recurse.update(new_rec)
        # the copy of recurse is shallow, so that the "inodes" dictionary is
        # always the same throughout one fileset
        if "inode" in values:
            inode = values["inode"]
            if inode in self.recurse["inodes"]:
                self.values["target"] = self.recurse["inodes"][inode]
            else:
                self.recurse["inodes"][inode] = self

    def __fspath__(self):
        return self.path

    def __repr__(self):
        return str([self.path, self.path_type, self.values, self.recurse])

    def get_type(self):
        return self.path_type

    def get_mode(self):
        """
        Get the file access rights according to file type and current settings
        """
        assert self.path_type in ["file", "directory"], (
            "Type {pt} can't get a mode".format(pt=self.path_type))
        generic = "mode"
        if self.path_type == "file":
            specific = "fmode"
        elif self.path_type == "directory":
            specific = "dmode"
        default = self.defaults.get(specific)

        mode = self.values.get(
            specific, self.values.get(
                generic, self.recurse.get(
                    specific, self.recurse.get(
                        generic, default))))

        if isinstance(mode, int):
            return mode
        else:
            return int(mode, base=8)

    def get(self, param):
        """
        Get the value of the given parameters across own values, recursive ones
        and potential default value.
        Returns None as last resort.
        """
        default = self.defaults.get(param)
        return self.values.get(param, self.recurse.get(param, default))

    def is_hardlinked(self):
        return self.get_type() != "link" and "target" in self.values


# --- INTERNAL FUNCTIONS ---


def _create_fileset(fullname, struct, recurse={}):
    """
    Recursive part of the fileset creation
    """
    set_path = SetPath(fullname, struct, recurse, struct.get("rec", {}))
    if set_path.get_type() == "directory":
        _create_directory(set_path, always_delete=True)
        for name in struct.get("contents", {}):
            _create_fileset(os.path.join(fullname, name),
                            struct["contents"][name], set_path.recurse)
        _finish_directory(set_path)
    else:
        if set_path.is_hardlinked():
            _create_hardlink(set_path)
        elif set_path.get_type() == "link":  # symlink
            _create_symlink(set_path)
        else:  # this must be a file
            _create_file(set_path)
        # other types of items are ignored for now


def _create_directory(set_path, always_delete=False):
    """
    Create a directory according to settings.

    It is first destroyed if requested.
    It is currently the recommended approach to make sure the
    structure is exactly as expected (a delta mechanism could be added).
    """
    if os.path.exists(set_path):
        if always_delete or not os.path.isdir(set_path):
            _rmtree(set_path)
        else:
            return
    os.makedirs(set_path)


def _finish_directory(set_path):
    """
    The directory is chmod according to "mode", _after_ the contained elements
    have been created.
    """
    os.chmod(set_path, set_path.get_mode())


def _create_file(set_path, always_delete=False):
    """
    Creates a file according to set_path

    The file will have the access rights according to "mode", and the "content"
    from the corresponding key, written in binary mode if "open" is set to "b",
    else "t".
    """
    if os.path.exists(set_path):
        if always_delete or not os.path.isfile(set_path):
            _rmtree(set_path)
    with open(set_path, "w" + set_path.get("open")) as fd:
        fd.write(set_path.get("content"))
    os.chmod(set_path, set_path.get_mode())


def _create_hardlink(set_path):
    """
    Creates a hardlink according to set_path
    """
    os.sync()
    os.link(set_path.get("target"), set_path)


def _create_symlink(set_path):
    """
    Creates a symlink according to set_path
    """
    os.symlink(set_path.get("target"), set_path)


def _rmtree(set_path):
    """
    Remove a complete tree making sure that access rights don't get in the way
    """
    for dir_name, dirs, files in os.walk(set_path):  # topdown
        mode = os.stat(dir_name).st_mode
        os.chmod(dir_name, mode | 0o222)
        # Windows can't remove read-only files
        for file_name in files:
            file = os.path.join(dir_name, file_name)
            mode = os.stat(file).st_mode
            os.chmod(file, mode | 0o222)
    shutil.rmtree(set_path)


def _compare_files(file1, stat1, file2, stat2):
    """
    Compares two files and their file stats.

    Those files are compared for type, link numbers, size and/or content,
    access mode, uid, gid.

    The result is a list of explained differences, empty if there are no
    differences.
    """
    differences = []

    if stat.S_IFMT(stat1.st_mode) != stat.S_IFMT(stat2.st_mode):
        differences.append(
            "Paths {pa1} and {pa2} have different types {ft1} vs. {ft2}".format(
                pa1=file1, pa2=file2,
                ft1=stat.S_IFMT(stat1.st_mode),
                ft2=stat.S_IFMT(stat2.st_mode)))
        # if the files don't have the same type, there is no point comparing
        # them further...
        return differences

    if stat.S_IMODE(stat1.st_mode) != stat.S_IMODE(stat2.st_mode):
        differences.append(
            "Paths {pa1} and {pa2} have different "
            "access rights {ar1} vs. {ar2}".format(
                pa1=file1, pa2=file2,
                ar1=stat.S_IMODE(stat1.st_mode),
                ar2=stat.S_IMODE(stat2.st_mode)))

    if stat1.st_nlink != stat2.st_nlink:
        differences.append(
            "Paths {pa1} and {pa2} have different "
            "link numbers {ln1} vs. {ln2}".format(
                pa1=file1, pa2=file2, ln1=stat1.st_nlink, ln2=stat2.st_nlink))

    if stat1.st_size != stat2.st_size:
        differences.append(
            "Paths {pa1} and {pa2} have different "
            "file sizes {fs1} vs. {fs2}".format(
                pa1=file1, pa2=file2, fs1=stat1.st_size, fs2=stat2.st_size))
    elif stat.S_ISREG(stat1.st_mode) and stat.S_ISREG(stat2.st_mode):
        with open(file1) as fd1, open(file2) as fd2:
            content1 = fd1.read()
            content2 = fd2.read()
        if content1 != content2:
            if len(content1) > 75:
                content1 = content1[:36] + "..." + content1[-36:]
            if len(content2) > 75:
                content2 = content2[:36] + "..." + content2[-36:]
            differences.append(
                "Paths {pa1} and {pa2} have different "
                "regular file' contents '{rc1}' vs. '{rc2}'".format(
                    pa1=file1, pa2=file2, rc1=content1, rc2=content2))

    # we compare only the modification seconds, because rdiff-backup doesn't
    # save milliseconds.
    if int(stat1.st_mtime) != int(stat2.st_mtime):
        differences.append(
            "Paths {pa1} and {pa2} have different "
            "modification times {mt1} vs. {mt2}".format(
                pa1=file1, pa2=file2,
                mt1=int(stat1.st_mtime), mt2=int(stat2.st_mtime)))

    if stat1.st_uid != stat2.st_uid:
        differences.append(
            "Paths {pa1} and {pa2} have different "
            "user owners {uo1} vs. {uo2}".format(
                pa1=file1, pa2=file2, uo1=stat1.st_uid, uo2=stat2.st_uid))

    if stat1.st_gid != stat2.st_gid:
        differences.append(
            "Paths {pa1} and {pa2} have different "
            "group owners {go1} vs. {go2}".format(
                pa1=file1, pa2=file2, go1=stat1.st_uid, go2=stat2.st_uid))

    return differences


if __name__ == "__main__":
    # just doing some minimal test when called as script
    # requires `tree` to work!
    import subprocess
    import tempfile

    base_temp_dir = tempfile.mkdtemp(".d", "fileset_")
    structure = {
        "a_dir": {
            "rec": {"fmode": 0o444, "content": "default content"},
            "contents": {
                "fileA": {"content": "initial", "inode": 0},
                "fileB": {"mode": "0544", "inode": "B"},
                "fileC": {"target": "../a_bin_file"},
                "fileD": {"inode": "B"},
            }
        },
        "empty_dir": {"type": "dir", "dmode": 0o777},
        "a_bin_file": {"content": b"some_binary_content", "open": "b"},
    }

    print("base directory: {bd}".format(bd=base_temp_dir))
    create_fileset(base_temp_dir, structure)
    subprocess.call(["tree", "-aJps", "--inodes", base_temp_dir])
    remove_fileset(base_temp_dir, structure)

"""
$ tree -aJps --inodes /tmp/fileset_eh24xnxy.d
[
  {"type":"directory","name":"/tmp/fileset_eh24xnxy.d","inode":0,"mode":"0700","prot":"drwx------","size":100,"contents":[
    {"type":"file","name":"a_bin_file","inode":190,"mode":"0644","prot":"-rw-r--r--","size":19},
    {"type":"directory","name":"a_dir","inode":185,"mode":"0755","prot":"drwxr-xr-x","size":120,"contents":[
      {"type":"file","name":"fileA","inode":186,"mode":"0444","prot":"-r--r--r--","size":7},
      {"type":"file","name":"fileB","inode":187,"mode":"0544","prot":"-r-xr--r--","size":15},
      {"type":"link","name":"fileC","target":"../a_bin_file","inode":190,"mode":"0777","prot":"lrwxrwxrwx","size":13},
      {"type":"file","name":"fileD","inode":187,"mode":"0544","prot":"-r-xr--r--","size":15}
  ]},
    {"type":"directory","name":"empty_dir","inode":189,"mode":"0777","prot":"drwxrwxrwx","size":40}
  ]}
,
  {"type":"report","directories":2,"files":5}
]
"""
