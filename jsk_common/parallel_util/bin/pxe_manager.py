#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright (C) 2011 Ryohei Ueda
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import BaseHTTPServer
import cgi
import sqlite3
import socket
import binascii
import os
from subprocess import check_call
from optparse import OptionParser
from string import Template

FIND_HOST_SQL = """
select * from hosts where hostname = '${hostname}';
"""

DB_CREATE_TABLE_SQL = """
create table hosts (
hostname text,
ip text,
macaddress text UNIQUE,
root text
);
"""

ADD_HOST_SQL = """
insert into hosts values ('${hostname}', '${ip}', '${macaddress}', '${root}');
"""

DEL_HOST_SQL = """
delete from hosts where hostname = '${hostname}';
"""

ALL_HOSTS_SQL = """
select * from hosts;
"""

APT_PACKAGES = """
ssh zsh bash-completion
emacs vim wget
build-essential subversion git-core cvs
cmake ethtool python python-paramiko python-meminfo-total
initramfs-tools linux-image nfs-kernel-server
ubuntu-desktop-ja gdm
"""

DHCP_SUBNET_TMPL = """
subnet ${subnet} netmask ${netmask} {
range dynamic-bootp ${dhcp_range_start} ${dhcp_range_stop};
option broadcast-address ${broadcast};
option domain-name-servers ${dns_ip};
option domain-name "${domain_name}";
option routers ${gateway};
filename "${pxe_filename}";
next-server ${pxe_server};
${hosts}
}
"""

DHCP_HOST_TMPL = """
  host ${hostname}{
   hardware ethernet ${mac};
   fixed-address ${ip};
   option host-name "${hostname}";
  }
"""

HTML_TMPL = """
<html>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN"
 "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="ja" lang="ja">
  <head>
    <meta http-equiv="Content-Type" content="text/html;charset=UTF-8" />
    <meta http-equiv="Content-Script-Type" content="text/javascript" />
    <title>PXE Manager</title>
    <style type="text/css">
dl.host_list {
 border:1px solid #999;
 width:490px;
}

dt.hostname {
 float:left;
 width:100px;
 padding:5px 0 5px 10px;
 clear:both;
 font-weight:bold;
}

dd.ip_mac {
 width:260px;
 margin-left:100px;
 padding:5px 5px 5px 10px;
 border-left:1px solid #999;
}

dl.ip_mac {
    width:260px;
    margin-left:0px;
    padding:5px 5px 5px 10px;
}
    </style>
  </head>
  <body>
    <div id="main">
      <div id="title">
        <h1>
          PXE Boot manager @ MBA
        </h1>
      </div> <!-- #title -->
      <div id="add">
       <form method="get" action="add">
         hostname
         <input type="text" name="hostname"/>
         IP
         <input type="text" name="ip"/>
         MACAddress
         <input type="text" name="macaddress"/>
         root directory
         <input type="text" name="root"/>
         <input type="submit" />
       </form>
      </div>
      <div id="host_list">
        <dl class="host_list">
          ${hosts}
        </dl>
      </div>
    </div>
  </body>
</html>
"""

HTML_HOST_TMPL = """
          <dt class="hostname">${hostname}</dt>
          <dd class="ip_mac">
            <div>
              ${ip}/${macaddress}@${root}
            </div>
            <div class="delete_host">
              <form method="get" action="delete">
                <input type="submit" value="delete" name="${hostname}"/>
              </form>
            </div>
          </dd>
"""

PXE_CONFIG_TMPL = """
menu INCLUDE pxelinux.cfg/graphics.cfg
DEFAULT vesamenu.c32
NOESCAPE 1
ALLOWOPTIONS 0
boot label in ${tftp_dir}
LABEL Lucid
      MENU LABEL Lucid-${hostname}
      MENU DEFAULT
      KERNEL ${root}/vmlinuz
      APPEND quiet splash initrd=${root}/initrd.img netboot=nfs raid=noautodetect root=/dev/nfs nfsroot=192.168.101.182:${tftp_dir}/${root} ip=dhcp rw --
"""

INITRAMFS_CONF = """
MODULES=netboot
BUSYBOX=y
COMPCACHE_SIZE=""
BOOT=nfs
DEVICE=eth0
NFSROOT=auto
"""

INITRAM_MODULES = """
sky2
e1000
tg3
bnx2
"""

FSTAB = """
proc /proc proc defaults 0 0
/dev/nfs / nfs defaults 1 1
none /tmp tmpfs defaults 0 0
none /var/run tmpfs defaults 0 0
none /var/lock tmpfs defaults 0 0
none /var/tmp tmpfs defaults 0 0
"""

def parse_options():
    parser = OptionParser()
    parser.add_option("--db", dest = "db",
                      default = os.path.join(os.path.dirname(__file__),
                                             "pxe.db"),
                      help = """db file to configure pxe boot environment.
(defaults to pxe.db)""")
    parser.add_option("--add", dest = "add", nargs = 3,
                      metavar = "HOSTNAME MACADDRESS IP ROOT_DIR",
                      help = """add new machine to db.
ROOT_DIR is a relative path from the directory specified by
--tftp-dir option.""")
    parser.add_option("--tftp-dir", dest = "tftp_dir",
                      default = "/data/tftpboot",
                      help = """root directory of tftpboot. defaults
to /data/tftpboot""")
    parser.add_option("--generate-pxe-config-files",
                      dest = "generate_pxe_config_files",
                      action = "store_true",
                      help = """ automatically generate the configuration files
under pxelinux.cfg/""")
    parser.add_option("--web", dest = "web",
                      action = "store_true", help = """run webserver""")
    parser.add_option("--web-port", dest = "web_port",
                      type = int,
                      default = 4040,
                      help = """port of webserver (defaults to 4040)""")
    parser.add_option("--delete", dest = "delete", nargs = 1,
                      help = """delete a host from db.""")
    parser.add_option("--generate-dhcp", dest = "generate_dhcp",
                      action = "store_true",
                      help = "generate dhcp file")
    parser.add_option("--dhcp-conf-file", dest = "dhcp_conf_file",
                      default = "/etc/dhcp3/dhcp.conf",
                      help = "dhcp configuration file to be overwritten")
    parser.add_option("--overwrite-dhcp", dest = "overwrite_dhcp",
                      action = "store_true",
                      help = """overwrite dhcp configuration file or not.
(defaults to false)""")
    parser.add_option("--prefix-dhcp-file", dest = "prefix_dhcp_file",
                      default = os.path.join(os.path.dirname(__file__),
                                             "dhcpd.conf.pre"),
                      help = """pxe_manager only generates host syntax.
if you want to create a full dhcpd.conf, you can specify a file to be a
prefix of dhcpd.conf""")
    parser.add_option("--pxe-filename", dest = "pxe_filename",
                      default = "pxelinux.0",
                      help = "file name of pxelinux.")
    parser.add_option("--pxe-server", dest = "pxe_server",
                      default = "192.168.101.153",
                      help = "the ip address of pxe server.")
    parser.add_option("--subnet", dest = "subnet",
                      default = "192.168.101.0",
                      help = "subnet of dhcp")
    parser.add_option("--netmask", dest = "netmask",
                      default = "255.255.255.0",
                      help = "netmask of dhcp")
    parser.add_option("--dhcp-range-start", dest = "dhcp_range_start",
                      default = "192.168.101.1",
                      help = "the starting ip address of dhcp")
    parser.add_option("--dhcp-range-stop", dest = "dhcp_range_stop",
                      default = "192.168.101.127",
                      help = "the ending ip address of dhcp")
    parser.add_option("--broadcast", dest = "broadcast",
                      default = "192.168.101.255",
                      help = "broadcast of the network")
    parser.add_option("--dns-ip", dest = "dns_ip",
                      default = "192.168.96.209",
                      help = "DNS of the network")
    parser.add_option("--domain-name", dest = "domain_name",
                      default = "jsk.t.u-tokyo.ac.jp",
                      help = "domain name of the network")
    parser.add_option("--gateway", dest = "gateway",
                      default = "192.168.101.254",
                      help = "gateway of the network")
    parser.add_option("--list", dest = "list",
                      action = "store_true",
                      help = "print the list of the machines registered")
    parser.add_option("--wol", dest = "wol",
                      metavar = "HOSTNAME",
                      action = "append",
                      help = """send magick packet of WakeOnLan to the
specified host""")
    parser.add_option("--wol-port", dest="wol_port",
                      type = int,
                      default = 9,
                      help = "port of WakeOnLan")
    parser.add_option("--generate-pxe-filesystem",
                      dest = "generate_pxe_filesystem",
                      nargs = 1)
    parser.add_option("--pxe-filesystem-template",
                      dest = "pxe_filesystem_template",
                      default = "/data/tftpboot/root_template",
                      nargs = 1)
    parser.add_option("--pxe-filesystem-enable-japanese",
                      action = "store_false",
                      dest = "pxe_filesystem_enable_japanese")
    parser.add_option("--pxe-filesystem-apt-sources",
                      dest = "pxe_filesystem_apt_sources",
                      default = os.path.join(os.path.dirname(__file__),
                                             "sources.list"))
    parser.add_option("--pxe-user",
                      dest = "pxe_user",
                      default = "pxe")
    parser.add_option("--pxe-passwd",
                      dest = "pxe_passwd",
                      default = "pxe")
    parser.add_option("--root", dest = "root",
                      nargs = 1,
                      default = "/data/tf/root",
                      help = "NFS root directory")
    (options, args) = parser.parse_args()
    return options

def send_wol_magick_packet(macs, ipaddr, port):
    "http://www.emptypage.jp/gadgets/wol.html"
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    for mac in macs:
        for sep in ':-':
            if sep in mac:
                mac = ''.join([x.rjust(2, '0') for x in mac.split(sep)])
                break
        mac = mac.rjust(12, '0')
        p = '\xff' * 6 + binascii.unhexlify(mac) * 16
        s.sendto(p, (ipaddr, port))
    s.close()
    
def open_db(db):
    if os.path.exists(db):
        con = sqlite3.connect(db)
    else:
        con = sqlite3.connect(db)
        con.execute(DB_CREATE_TABLE_SQL)
    return con

def delete_host(con, host):
    template = Template(DEL_HOST_SQL)
    sql = template.substitute({"hostname": host})
    con.execute(sql)
    con.commit()

def add_host(con, host, ip, mac, root):
    template = Template(ADD_HOST_SQL)
    sql = template.substitute({"hostname": host,
                               "ip": ip,
                               "macaddress": mac,
                               "root": root})
    con.execute(sql)
    con.commit()

def all_hosts(con):
    sql_result = con.execute(ALL_HOSTS_SQL)
    result = {}
    for row in sql_result:
        result[row[0]] = {"ip": row[1], "macaddress": row[2], "root": row[3]}
    return result

def find_by_hostname(con, hostname):
    template = Template(FIND_HOST_SQL)
    sql_str = template.substitute({"hostname": hostname})
    sql_result = con.execute(sql_str)
    for row in sql_result:
        return {"ip": row[1], "macaddress": row[2], "root": row[3]}
    raise "cannot find %s" % (hostname)

def find_machine_tag_by_hostname(dom, hostname):
    machines = dom.getElementsByTagName("machine")
    for m in machines:
        if m.getAttribute("name") == hostname:
            return m
    return False

def get_root_pxe(dom):
    return dom.childNodes[0]

def delete_machine(hostname, db):
    con = open_db(db)
    delete_host(con, hostname)
    con.close()
    
def add_machine(hostname, mac, ip, root, db):
    con = open_db(db)
    add_host(con, hostname, ip, mac, root)
    con.close()
    
def generate_dhcp(options, db):
    con = open_db(db)
    machines = all_hosts(con)
    generated_string = []
    # generate host section
    for (hostname, ip_mac) in machines.items():
        ip = ip_mac["ip"]
        mac = ip_mac["macaddress"]
        template = Template(DHCP_HOST_TMPL)
        host_str = template.substitute({"hostname": hostname,
                                        "ip": ip,
                                        "mac": mac})
        generated_string.append(host_str)
    if options.prefix_dhcp_file:
        f = open(options.prefix_dhcp_file)
        prefix_str = "".join(f.readlines())
    else:
        prefix_str = ""
    # generate dhcp subnet section
    dhcp_subnet_tmpl = Template(DHCP_SUBNET_TMPL)
    replace_dict = {"subnet": options.subnet,
                    "netmask": options.netmask,
                    "dhcp_range_start": options.dhcp_range_start,
                    "dhcp_range_stop": options.dhcp_range_stop,
                    "broadcast": options.broadcast,
                    "dns_ip": options.dns_ip,
                    "domain_name": options.domain_name,
                    "gateway": options.gateway,
                    "pxe_server": options.pxe_server,
                    "pxe_filename": options.pxe_filename,
                    "hosts": "\n".join(generated_string)}
    dhcp_subnet_str = dhcp_subnet_tmpl.substitute(replace_dict)
    if options.overwrite_dhcp:
        path = options.dhcp_conf_file
        f = open(path, "w")
        f.write(prefix_str + dhcp_subnet_str)
        f.close()
    else:
        print prefix_str + dhcp_subnet_str

def print_machine_list(db):
    con = open_db(db)
    machines = all_hosts(con)
    for hostname, ip_mac in machines.items():
        ip = ip_mac["ip"]
        mac = ip_mac["macaddress"]
        root = ip_mac["root"]
        print """%s:
  ip: %s
  mac: %s
  root: %s
""" % (hostname, ip, mac, root)

def wake_on_lan(hostname, port, broadcast, db):
    con = open_db(db)
    mac = find_by_hostname(db, hostname)["macaddress"]
    send_wol_magick_packet([mac], broadcast, port)

def chroot_command(chroot_dir, *args):
    command = ["chroot", chroot_dir]
    command.extend(*args)
    print command
    return check_call(command)
    
def generate_pxe_template_filesystem(template_dir):
    print ">>> generating template filesystem"
    try:
        check_call(["debootstrap", "lucid", template_dir])
    except:
        # remove template_dir
        print ">>> removing template dir"
        check_call(["rm", "-rf", template_dir])
        raise


def copy_template_filesystem(template_dir, target_dir, apt_sources):
    print ">>> copying filesystem"
    check_call(["cp", "-ax", template_dir, target_dir])
    print ">>> copying etc/apt/sources.list"
    check_call(["cp", apt_sources,
                os.path.join(target_dir, "etc", "apt", "sources.list")])
    return

class ChrootEnvironment():
    def __init__(self, target_dir):
        self.target_dir = target_dir
    def __enter__(self):
        target_dir = self.target_dir
        check_call(["mount", "-o", "bind", "/dev/",
                    os.path.join(target_dir, "dev")])
        chroot_command(target_dir,
                       "mount -t proc none /proc".split())
        chroot_command(target_dir,
                       "mount -t sysfs none /sys".split())
        chroot_command(target_dir,
                       "mount -t devpts none /dev/pts".split())
    def __exit__(self):
        target_dir = self.target_dir
        chroot_command(target_dir,
                       "umount -lf /proc".split())
        chroot_command(target_dir,
                       "umount -lf /sys".split())
        chroot_command(target_dir,
                       "umount -lf /dev/pts".split())
        check_call(["umount", "-lf", os.path.join(target_dir, "dev")])
    

def install_apt_packages(target_dir):
    # first of all mount
    env = ChrootEnvironment(target_dir)
    with env:
        print ">>> installing apt packages"
        chroot_command(target_dir, ["sh", "-c",
                                    "wget -q https://www.ubuntulinux.jp/ubuntu-ja-archive-keyring.gpg -O- | sudo apt-key add -"])
        chroot_command(target_dir, ["sh", "-c",
                                    "wget -q https://www.ubuntulinux.jp/ubuntu-jp-ppa-keyring.gpg -O- | sudo apt-key add -"])
        chroot_command(target_dir, ["sh", "-c",
                                    "sudo wget https://www.ubuntulinux.jp/sources.list.d/lucid.list -O /etc/apt/sources.list.d/ubuntu-ja.list"])
        chroot_command(target_dir, ["apt-get", "update"])
        chroot_command(target_dir,
                       ["apt-get", "install", "--force-yes", "-y"] + APT_PACKAGES.split())
        print "  >>> installing ros apt sources"
        chroot_command(target_dir, ["sh", "-c",
                                    "echo deb http://packages.ros.org/ros/ubuntu lucid main > /etc/apt/sources.list.d/ros-latest.list"])
        chroot_command(target_dir, ["sh", "-c",
                                    "wget http://packages.ros.org/ros.key -O - | apt-key add -"])
        chroot_command(target_dir, ["apt-get", "update"])
        chroot_command(target_dir, ["apt-get", "install", "--force-yes", "-y", "ros-diamondback-ros-base"])

def setup_user(target_dir, user, passwd):
    try:
        check_call(["mount", "-o", "bind", "/dev/",
                    os.path.join(target_dir, "dev")])
        chroot_command(target_dir,
                       "mount -t proc none /proc".split())
        chroot_command(target_dir,
                       "mount -t sysfs none /sys".split())
        chroot_command(target_dir,
                       "mount -t devpts none /dev/pts".split())
        chroot_command(target_dir, ["sh", "-c",
                                    "id %s || useradd %s" % (user, user)])
        chroot_command(target_dir, ["sh", "-c",
                                    "echo %s:%s | chpasswd" % (user, passwd)])
    finally:
        chroot_command(target_dir,
                       "umount -lf /proc".split())
        chroot_command(target_dir,
                       "umount -lf /sys".split())
        chroot_command(target_dir,
                       "umount -lf /dev/pts".split())
        check_call(["umount", "-lf", os.path.join(target_dir, "dev")])

def update_initram(target_dir):
    try:
        check_call(["mount", "-o", "bind", "/dev/",
                    os.path.join(target_dir, "dev")])
        chroot_command(target_dir,
                       "mount -t proc none /proc".split())
        chroot_command(target_dir,
                       "mount -t sysfs none /sys".split())
        chroot_command(target_dir,
                       "mount -t devpts none /dev/pts".split())
        chroot_command(target_dir, ["sh", "-c",
                                    "echo '%s' > /etc/initramfs-tools/initramfs.conf" % (INITRAMFS_CONF)])
        chroot_command(target_dir, ["sh", "-c",
                                    "echo '%s' > /etc/fstab" % (FSTAB)])
        chroot_command(target_dir, ["sh", "-c",
                                    "echo '%s' > /etc/initramfs-tools/modules" % (INITRAM_MODULES)])
        chroot_command(target_dir, ["sh", "-c",
                                    "mkinitramfs -o /boot/initrd.img-`uname -r` `ls /lib/modules`"])
    finally:
        chroot_command(target_dir,
                       "umount -lf /proc".split())
        chroot_command(target_dir,
                       "umount -lf /sys".split())
        chroot_command(target_dir,
                       "umount -lf /dev/pts".split())
        check_call(["umount", "-lf", os.path.join(target_dir, "dev")])
        
def generate_pxe_filesystem(template_dir, target_dir, apt_sources,
                            user, passwd):
    if not os.path.exists(template_dir):
        generate_pxe_template_filesystem(template_dir)
    if not os.path.exists(target_dir):
        copy_template_filesystem(template_dir, target_dir, apt_sources)
    install_apt_packages(target_dir)
    setup_user(target_dir, user, passwd)
    update_initram(target_dir)

def generate_top_html(db):
    con = open_db(db)
    machines = all_hosts(con)
    host_strs = []
    for (hostname, ip_mac) in machines.items():
        ip = ip_mac["ip"]
        mac = ip_mac["macaddress"]
        root = ip_mac["root"]
        template = Template(HTML_HOST_TMPL)
        host_strs.append(template.substitute({"hostname": hostname,
                                              "ip": ip,
                                              "macaddress": mac,
                                              "root": root}))
    con.close()
    html_template = Template(HTML_TMPL)
    return html_template.substitute({"hosts": "\n".join(host_strs)})

def update_dhcp_from_web():
    if global_options.overwrite_dhcp:
        generate_dhcp(global_options, global_options.db)
    if global_options.generate_pxe_config_files:
        generate_pxe_config_files(global_options)

class WebHandler(BaseHTTPServer.BaseHTTPRequestHandler):
    def do_GET(s):
        s.send_response(200)
        s.send_header("Content-type", "text/html")
        s.end_headers()
        if s.path.startswith("/delete"): # delete
            delete_host = cgi.parse_qs(s.path.split("?")[1]).keys()[0]
            delete_machine(delete_host, db_name)
            update_dhcp_from_web()
        elif s.path.startswith("/add"): # add
            add_host_desc = cgi.parse_qs(s.path.split("?")[1])
            print add_host_desc
            add_machine(add_host_desc["hostname"][0],
                        add_host_desc["macaddress"][0],
                        add_host_desc["ip"][0],
                        add_host_desc["root"][0],
                        db_name)
            update_dhcp_from_web()
        html = generate_top_html(db_name)
        s.wfile.write(html)
    
def run_web(options):
    db = options.db
    port = options.web_port
    overwritep = options.overwrite_dhcp
    # setup global variables
    global db_name              # dirty hack!
    global global_options
    db_name = db
    global_options = options
    BaseHTTPServer.HTTPServer(('localhost', port), WebHandler).serve_forever()

def generate_pxe_config_files(options):
    if options.generate_pxe_config_files:
        print ">>> generate the pxe configuration files"
        db = options.db
        con = open_db(db)
        machines = all_hosts(con)
        for hostname in machines.keys():
            template = Template(PXE_CONFIG_TMPL)
            mac = machines[hostname]["macaddress"]
            file_name = os.path.join(options.tftp_dir, "pxelinux.cfg",
                                     "01-" + mac.replace(":", "-"))
            file_str = template.substitute({"hostname": hostname,
                                            "root": machines[hostname]["root"],
                                            "tftp_dir": options.tftp_dir})

            f = open(file_name, "w")
            f.write(file_str)
            f.close()
    
    
def main():
    options = parse_options()
    if options.web:
        run_web(options)
    else:
        if options.add:
            add_machine(options.add[0], options.add[1],
                        options.add[2], options.db)
        if options.delete:
            delete_machine(options.delete, options.db)
        if options.generate_dhcp:
            generate_dhcp(options,
                          options.db)
        if options.list:
            print_machine_list(options.db)
        if options.wol:
            for m in [options.wol]:
                wake_on_lan(m[0], options.wol_port,
                            options.broadcast, options.db)
        if options.generate_pxe_filesystem:
            generate_pxe_filesystem(options.pxe_filesystem_template,
                                    options.generate_pxe_filesystem,
                                    options.pxe_filesystem_apt_sources,
                                    options.pxe_user, options.pxe_passwd)
        if options.generate_pxe_config_files:
            generate_pxe_config_files(options)
            
        
if __name__ == "__main__":
    main()
