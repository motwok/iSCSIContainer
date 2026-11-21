import os


class Lio:
    """Everything that changes the kernel target goes through here (rtslib)."""

    def __init__(self, rtslib=None):
        if rtslib is None:
            import rtslib_fb as rtslib
        self.r = rtslib
        self.Error = rtslib.RTSLibError

    def clear(self):
        self.r.RTSRoot().clear_existing(confirm=True)

    def fileio(self, name, path, write_back, serial):
        return self.r.FileIOStorageObject(name, dev=path, size=os.path.getsize(path),
                                          write_back=write_back, wwn=serial)

    def block(self, name, path, serial):
        return self.r.BlockStorageObject(name, dev=path, wwn=serial)

    def delete(self, obj):
        obj.delete()

    def set_attribute(self, obj, key, value):
        obj.set_attribute(key, value)

    def set_parameter(self, tpg, key, value):
        tpg.set_parameter(key, value)

    def target(self, iqn):
        fabric = self.r.FabricModule("iscsi")
        return self.r.TPG(self.r.Target(fabric, iqn, mode="create"), 1, mode="create")

    def find_target(self, iqn):
        try:
            fabric = self.r.FabricModule("iscsi")
            return self.r.TPG(self.r.Target(fabric, iqn, mode="lookup"), 1, mode="lookup")
        except self.Error:
            return None

    def delete_target(self, tpg):
        tpg.parent_target.delete()

    def enable(self, tpg):
        tpg.enable = True

    def portal(self, tpg, address, port):
        if ":" in address:
            address = f"[{address}]"
        self.r.NetworkPortal(tpg, address, port, mode="create")

    def lun(self, tpg, number, storage):
        return self.r.LUN(tpg, number, storage)

    def acl(self, tpg, initiator):
        return self.r.NodeACL(tpg, initiator, mode="create")

    def find_acl(self, tpg, initiator):
        try:
            return self.r.NodeACL(tpg, initiator, mode="lookup")
        except self.Error:
            return None

    def map(self, acl, number, lun):
        self.r.MappedLUN(acl, number, lun)

    def set_chap(self, acl, user, password, mutual_user, mutual_password):
        acl.chap_userid = user or ""
        acl.chap_password = password or ""
        acl.chap_mutual_userid = mutual_user or ""
        acl.chap_mutual_password = mutual_password or ""
