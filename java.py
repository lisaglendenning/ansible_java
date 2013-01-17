#!/usr/bin/python

DOCUMENTATION = """
---
module: java
author: Lisa Glendenning
short_description: Manages installation of Oracle Java 7 on Linux
description:
    - Manage installation of Oracle Java 7 on Linux.
#requirements: null
#version_added: null
notes:
    - "Tested with Ansible v0.9."
    - "Tested on 64-bit Fedora 15 and Ubuntu 12."
options:
    state:
        description:
            - "whether to install JRE (I(jre)), install JDK (I(jdk)), or uninstall (I(none))"
        required: false
        default: jre
        choices: [none, jre, jdk]
    package_location:
        description:
            - "non-standard location for packages"
        required: false
        default: null
examples:
    - code: "java state=jdk"
      description: "Install the latest JDK."
"""

#############################################################################
#############################################################################

import os
import re
import platform
import tempfile
import traceback
from collections import namedtuple

# just for run_command hack
import subprocess

#############################################################################
#############################################################################

# Because it doesn't look like a module can call into other modules :(
# so there's some duplicated effort going on here

class Download(object):
    
    CMD = 'wget'

    def __init__(self, module):
        self.module = module
    
    def fetch(self, url, opts=None, destfile=None, destdir=None):
        if destdir is None:
            destdir = tempfile.gettempdir()
        if destfile is None:
            destfile = url.rsplit('/', 1)[1]
        dest = os.path.join(destdir, destfile)
        
        if opts is None:
            opts = ('-c', '--no-cookies')
        
        argv = [self.CMD]
        argv.extend(opts)
        argv.append(url)
        argv.append('--output-document=%s' % dest)
        
        result = self.module.run_command(argv)
        if result[0] != 0:
            raise RuntimeError('Error: Download returned %d: %s' % (result[0], argv))
        
        return dest
    
class Yum(object):
    
    def __init__(self, module):
        self.module = module
    
    def installed(self, name):
        args = r"rpm -qa --queryformat '%{NAME}\n' | grep '^" + name + r"$'"
        result = self.module.run_command(args)
        return result[0] == 0
    
    def install(self, name):
        if os.path.isfile(name):
            args = r"rpm -qp --queryformat '%{NAME}\n' " + name
            result = self.module.run_command(args, True)
            pkg = result[1].strip()
        else:
            pkg = name
        if self.installed(pkg):
            return False
        argv = ['yum', '-y', 'install', name]
        self.module.run_command(argv, True)
        return True
        
    def uninstall(self, name):
        if not self.installed(name):
            return False
        argv = ['yum', '-y', 'remove', name]
        self.module.run_command(argv, True)
        return True
    
class Apt(object):
    CMD = 'apt-get'
    PATH = '/usr/bin'
    ENV = {
           'DEBIAN_FRONTEND': 'noninteractive', 
           'DEBIAN_PRIORITY': 'critical',
           }
    
    @staticmethod
    def package_split(pkgspec):
        parts = pkgspec.split('=')
        if len(parts) > 1:
            return parts[0], parts[1]
        else:
            return parts[0], None
    
    @classmethod
    def args(cls):
        return [' '.join(['%s=%s' % kv for kv in cls.ENV.iteritems()]),
                os.path.join(cls.PATH, cls.CMD)]
    
    def __init__(self, module):
        self.module = module
    
    def status(self, name):
        fmt = r'\t'.join([r'${%s}' % s for s in ('package', 'version', 'status')]) + r'\n'
        args = r"dpkg-query -f '%s' -W '%s'" % (fmt, name)
        result = self.module.run_command(args)
        if result[1]:
            lines = result[1].splitlines()
            assert len(lines) == 1, result[1]
            return lines[0].split('\t')
        return None
    
    def installed(self, name):
        status = self.status(name)
        if not status:
            return False
        return status[2] == 'install ok installed'
    
    def install(self, pkgspec, upgrade=False, default_release=None, install_recommends=True, force=False):
        packages = []
        if isinstance(pkgspec, str):
            pkgspec = [pkgspec]
        for package in pkgspec:
            name, version = self.package_split(package)
            # FIXME: check version/upgrade
            if not self.installed(name):
                packages.append(package)
    
        result = None
        if packages:
            argv = self.args()
            argv.extend(['--option', 'Dpkg::Options::=--force-confold',
                         '-q', '-y'])
            if force:
                argv.append('--force-yes')
            if not install_recommends:
                argv.append('--no-install-recommends')
            if default_release:
                argv.append('-t')
                argv.append("'%s'" % default_release)
            argv.append('install')
            argv.extend(["'%s'" % p for p in packages])
    
            self.module.run_command(' '.join(argv), True)
            result = True
        return result
    
    def uninstall(self, pkgspec, purge=False):
        packages = []
        if isinstance(pkgspec, str):
            pkgspec = [pkgspec]
        for package in pkgspec:
            name, version = self.package_split(package)
            if self.installed(name):
                packages.append(package)
    
        result = None
        if packages:
            argv = self.args()
            argv.extend(['-q', '-y'])
            if purge:
                argv.append('--purge')
            argv.append('remove')
            argv.extend(["'%s'" % p for p in packages])
            
            self.module.run_command(' '.join(argv), True)
            result = True
        return result
    
    def update(self):
        argv = self.args()
        argv.extend(['-q', '-y'])
        argv.append('update')
        result = self.module.run_command(' '.join(argv), True)
        return result
    
class AptKey(object):
    CMD = 'apt-key'
    PATH = '/usr/bin'
    
    def __init__(self, module):
        self.module = module
        
    def installed(self, key):
        args = "%s list | grep '%s'" % (os.path.join(self.PATH, self.CMD), key)
        result = self.module.run_command(args)
        return len(result[1])
    
    def install(self, key):
        if self.installed(key):
            return False
        argv = [os.path.join(self.PATH, self.CMD), 'adv',
                '--keyserver', 'keys.gnupg.net', '--recv-keys', key]
        self.module.run_command(argv, True)
        return True
    
    def uninstall(self, key):
        if not self.installed(key):
            return False
        argv = [os.path.join(self.PATH, self.CMD), 'del', key]
        self.module.run_command(argv, True)
        return True
        
class AptRepository(object):
    PACKAGE = 'python-software-properties'
    CMD = 'add-apt-repository'
    PPA_SERVER = 'http://ppa.launchpad.net/'
    
    @classmethod
    def args(cls):
        args = [cls.CMD]
        if platform.dist()[0] == 'debian' or float(platform.dist()[1]) >= 11.10:
            args.append('-y')
        return args
    
    def __init__(self, module):
        self.module = module
        self.apt = Apt(self.module)
        self.apt.install(self.PACKAGE)
    
    def installed(self, repo):
        args = "egrep -v '^#|^ *$' /etc/apt/sources.list /etc/apt/sources.list.d/*.list"
        result = self.module.run_command(args)
        for output in result[1].splitlines():
            filename, line = output.split(':', 1)
            if repo.split()[0] in ('deb', 'deb-src'):
                if line == repo:
                    return True
            elif repo.startswith('ppa:'):
                url = self.PPA_SERVER + repo[4:] + '/'
                if line.split()[1].startswith(url):
                    return True
            else:
                raise NotImplementedError(repo)
        return False
    
    def install(self, repo):
        if self.installed(repo):
            return None
        argv = self.args()
        argv.append(repo)
        self.module.run_command(argv, True)
        self.apt.update()
        return True
    
    def uninstall(self, repo):
        if not self.installed(repo):
            return None
        argv = self.args()
        argv.append('--remove')
        argv.append(repo)
        self.module.run_command(argv, True)
        self.apt.update()
        return True

#############################################################################
#############################################################################

class JavaVersion(namedtuple('JavaVersion', 'major, minor, release, build')):
    MAJOR_PATTERN = r'(?P<major>[0-7])'
    MINOR_PATTERN = r'(?P<minor>\d+)'
    RELEASE_PATTERN = r'(?P<release>\d+)'
    BUILD_PATTERN = r'(?:-b(?P<build>\d+))'
    VERSION_PATTERN = r'^(?:1\.)?%s(?:\.%s(?:_%s%s?)?)?$' \
        % (MAJOR_PATTERN, MINOR_PATTERN, RELEASE_PATTERN, BUILD_PATTERN)
    UPDATE_PATTERN = r'^%s(?:u%s%s?)?$' \
        % (MAJOR_PATTERN, RELEASE_PATTERN, BUILD_PATTERN)
        
    UPDATE_TEMPLATE = r'%(major)su%(release)s'
    BUILD_TEMPLATE = UPDATE_TEMPLATE + '-b%(build)s'
    
    @classmethod
    def from_string(cls, text):
        if not text:
            return None

        for pattern in (cls.VERSION_PATTERN, cls.UPDATE_PATTERN):
            m = re.match(pattern, text)
            if m is not None:
                break
        else:
            return None

        fields = m.groupdict()
        for k,v in fields.iteritems():
            if v is not None:
                fields[k] = int(v)
        return cls(**fields)
    
    def __new__(cls, major=None, minor=None, release=None, build=None):
        return super(JavaVersion, cls).__new__(cls, major, minor, release, build)
    
    def update_string(self):
        return self.UPDATE_TEMPLATE % self._asdict()
    
    def build_string(self):
        return self.BUILD_TEMPLATE % self._asdict()

#############################################################################
#############################################################################

class Java(object):
    
    subclasses = {}
    
    arguments = {
        'state': {'default': 'jre', 'choices': ['none', 'jre', 'jdk',],},
        'version': {'default': '7', 'choices': ['7',],},
        'vendor': {'default': 'Oracle', 'choices': ['Oracle',],},
        'package_location': {'default': None,}
    }
    
    @classmethod
    def discover_version(cls, module, jdk=False):
        version = None
        if jdk:
            args = ' | '.join(("javac 2>&1 -version",
                               "grep '^javac '",
                               "cut -f 2 -s -d ' '",))
        else:
            args = ' | '.join(("java 2>&1 -version",
                               "grep 'java version'",
                               "cut -f 3 -s -d ' '",
                               "cut -d '\"' -f 2",))
        output = module.run_command(args)
        if output[1]:
            text = output[1].strip()
            version = JavaVersion.from_string(text)
            if version is None:
                raise RuntimeError("'Unable to parse Java version '%s'" % text)
        return version
    
    @classmethod
    def discover_arch(cls):
        return platform.machine()
    
    @classmethod
    def select_subclass(cls):
        plat = platform.system()
        if plat == 'Linux':
            try:
                dist = platform.linux_distribution()[0].capitalize()
            except:
                # FIXME: MethodMissing, I assume?
                dist = platform.dist()[0].capitalize()
        else:
            raise RuntimeError('Platform %s not supported' % plat)

        subcls = None
        for dists, subcls in cls.subclasses.iteritems():
            if dist in dists:
                break
        else:
            raise RuntimeError('Distribution %s not supported' % dist)
        return subcls
    
    @classmethod
    def main(cls, module, *args, **kwargs):
        subcls = cls.select_subclass()
        self = subcls(module, *args, **kwargs)
        return self.apply()

    def __init__(self, module):
        self.module = module
    
    def install(self, target_state, target_version):
        raise NotImplementedError
    
    def uninstall(self):
        raise NotImplementedError
    
    def apply(self):
        module = self.module
        
        result = {
            'changed': False,
        }

        current_state = 'none'
        current_version = self.discover_version(module, True)
        if current_version:
            current_state = 'jdk'
        else:
            current_version = self.discover_version(module, False)
            if current_version:
                current_state = 'jre'
        
        target_state = module.params['state']
        if target_state == 'none':
            target_version = None
        else:
            target_version = JavaVersion.from_string(module.params['version'])
            if target_version is None:
                raise ValueError("Unable to parse Java version '%s'" % module.params['version'])
        
        # are we done?
        if current_state == target_state:
            # check version
            if current_state != 'none':
                if current_version >= target_version:
                    return result
            else:
                return result
        
        # uninstall current java
        if current_state != 'none':
            result['changed'] = bool(self.uninstall()) or result['changed']
        
        if target_state != 'none':
            result['changed'] = bool(self.install(target_state, target_version)) or result['changed']
        
        return result

#############################################################################
#############################################################################

class JavaApt(Java):
    
    JDK_REPO = 'ppa:webupd8team/java'
    JDK_PACKAGE = 'oracle-java7-installer'
    
    JRE_REPO = 'deb http://www.duinsoft.nl/pkg debs all'
    JRE_REPO_KEY = '5CB26B26'
    JRE_REPO_FILE = '/etc/apt/sources.list.d/duinsoft.list'
    JRE_PACKAGE = 'update-sun-jre'
    
    def __init__(self, module):
        super(JavaApt, self).__init__(module)
        self.apt = Apt(module)
        self.aptrepo = AptRepository(module)

    def install_jdk(self, target_version):
        changed = self.aptrepo.install(self.JDK_REPO)
        if not self.apt.installed(self.JDK_PACKAGE):
            # accept Oracle license
            args = "echo oracle-java7-installer shared/accepted-oracle-license-v1-1 select true | /usr/bin/debconf-set-selections"
            self.module.run_command(args, True)
        changed = self.apt.install(self.JDK_PACKAGE) or changed
        return changed
    
    def uninstall_jdk(self):
        changed = self.apt.uninstall(self.JDK_PACKAGE)
        changed = self.aptrepo.uninstall(self.JDK_REPO) or changed
        return changed
        
    def install_jre(self, target_version):
        changed = False
        if not os.path.isfile(self.JRE_REPO_FILE):
            with open(self.JRE_REPO_FILE, 'w') as f:
                f.write(self.JRE_REPO)
                f.write('\n')
            changed = True
    
        aptkey = AptKey(self.module)
        changed = aptkey.install(self.JRE_REPO_KEY) or changed
        if changed:
            self.apt.update()
        
        changed = self.apt.install(self.JRE_PACKAGE) or changed
        return changed
        
    def uninstall_jre(self):
        changed = self.apt.uninstall(self.JRE_PACKAGE)
        if os.path.isfile(self.JRE_REPO_FILE):
            os.remove(self.JRE_REPO_FILE)
            changed = True
            self.apt.update()
            
        aptkey = AptKey(self.module)
        changed = aptkey.uninstall(self.JRE_REPO_KEY) or changed

        return changed
    
    def install(self, target_state, target_version):
        if target_state == 'jdk':
            changed = self.install_jdk(target_version)
        elif target_state == 'jre':
            changed = self.install_jre(target_version)
        else:
            raise ValueError(target_state)
        return changed
        
    def uninstall(self):
        changed = self.uninstall_jdk()
        changed = self.uninstall_jre() or changed
        return changed
    
super(JavaApt, JavaApt).subclasses[('Ubuntu',)] = JavaApt

#############################################################################
#############################################################################

class JavaYum(Java):
    # FIXME: get latest versions dynamically
    LATEST_VERSION = JavaVersion(7, 0, 11, 21)
    
    JDK_URL = 'http://download.oracle.com/otn-pub/java/jdk/'
    JDK_COOKIE = 'Cookie: gpw_e24=http%3A%2F%2Fwww.oracle.com%2F'
    
    JRE_X64_URL = 'http://javadl.sun.com/webapps/download/AutoDL?BundleId=73133'
    JRE_X86_URL = 'http://javadl.sun.com/webapps/download/AutoDL?BundleId=73131'
    
    def __init__(self, module):
        super(JavaYum, self).__init__(module)
        self.yum = Yum(module)
        self.download = Download(module)
        
    def uninstall_jdk(self):
        return self.yum.uninstall('jdk')
    
    def uninstall_jre(self):
        return self.yum.uninstall('jre')
    
    def install_jdk(self, target_version):
        # see https://forums.oracle.com/forums/thread.jspa?messageID=10563534
        version = self.LATEST_VERSION
        arch = 'x64' if self.discover_arch() == 'x86_64' else 'i586'
        filename = 'jdk-%s-linux-%s.rpm' % (version.update_string(), arch)
        
        source = None
        if self.module.params['package_location']:
            source = self.module.params['package_location']
        else:
            source = '%s%s/' % (self.JDK_URL, version.build_string())
        if source.endswith('/'):
            source += filename
        
        dest = None
        if source.startswith('/'): # assume local file
            dest = source
        else: # assume url
            opts = ('-c', '--no-cookies', '--header', self.JDK_COOKIE,)
            dest = self.download.fetch(source, opts=opts, destfile=filename)
        
        self.yum.install(dest)
        return True
      
    def install_jre(self, target_version):
        version = self.LATEST_VERSION
        arch = 'x64' if self.discover_arch() == 'x86_64' else 'i586'
        filename = 'jre-%s-linux-%s.rpm' % (version.update_string(), arch)
        
        source = None
        if self.module.params['package_location']:
            source = self.module.params['package_location']
        else:
            source = self.JRE_X64_URL if arch == 'x64' else self.JRE_X86_URL
        if source.endswith('/'):
            source += filename

        dest = None
        if source.startswith('/'): # assume local file
            dest = source
        else: # assume url
            dest = self.download.fetch(source, destfile=filename)
            
        self.yum.install(dest)
        return True
        
    def install(self, target_state, target_version):
        if target_state == 'jdk':
            changed = self.install_jdk(target_version)
        elif target_state == 'jre':
            changed = self.install_jre(target_version)
        else:
            raise ValueError(target_state)
        return changed
        
    def uninstall(self):
        changed = self.uninstall_jdk()
        changed = self.uninstall_jre() or changed
        return changed
    
super(JavaYum, JavaYum).subclasses[('Fedora',)] = JavaYum

#############################################################################
#############################################################################

# hack because Ansible 0.9 doesn't include this function

def run_command(self, args, check_rc=False, close_fds=False, executable=None):
    '''
    Execute a command, returns rc, stdout, and stderr.
    args is the command to run
    If args is a list, the command will be run with shell=False.
    Otherwise, the command will be run with shell=True when args is a string.
    Other arguments:
    - check_rc (boolean)  Whether to call fail_json in case of
                          non zero RC.  Default is False.
    - close_fds (boolean) See documentation for subprocess.Popen().
                          Default is False.
    - executable (string) See documentation for subprocess.Popen().
                          Default is None.
    '''
    if isinstance(args, list):
        shell = False
    elif isinstance(args, basestring):
        shell = True
    else:
        msg = "Argument 'args' to run_command must be list or string"
        self.fail_json(rc=257, cmd=args, msg=msg)
    rc = 0
    msg = None
    try:
        cmd = subprocess.Popen(args,
                               executable=executable,
                               shell=shell,
                               close_fds=close_fds,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = cmd.communicate()
        rc = cmd.returncode
    except (OSError, IOError), e:
        self.fail_json(rc=e.errno, msg=str(e), cmd=args)
    except:
        self.fail_json(rc=257, msg=traceback.format_exc(), cmd=args)
    if rc != 0 and check_rc:
        msg = err.rstrip()
        self.fail_json(cmd=args, rc=rc, stdout=out, stderr=err, msg=msg)
    return (rc, out, err)

def main():
    # hack for 0.9
    AnsibleModule.run_command = run_command
    mod = AnsibleModule(argument_spec=Java.arguments)
    try:
        result = Java.main(mod)
    except Exception:
        msg = traceback.format_exc()
        mod.fail_json(msg=msg)
    else:
        mod.exit_json(**result)

# include magic from lib/ansible/module_common.py
#<<INCLUDE_ANSIBLE_MODULE_COMMON>>
main()
