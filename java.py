#!/usr/bin/python

DOCUMENTATION = """
---
module: java
author: Lisa Glendenning
short_description: Installs Oracle Java 7 on Ubuntu/Fedora.
description:
    - Installs Oracle Java 7 on Ubuntu/Fedora.
requirements:
    - wget
    - alternatives, update-alternatives
#version_added: null
notes:
    - "Tested with Ansible v1.3."
    - "Tested on 64-bit Fedora 14."
    - "Undefined behavior if mixed with other Java installations."
options:
    state:
        description:
            - "whether to install JRE (C(jre)), install JDK (C(jdk)), or uninstall (C(none))"
        required: false
        default: jre
        choices: [none, jre, jdk]
    package_location:
        description:
            - "non-standard URL or filesystem path to Java packages"
        required: false
        default: None
"""

EXAMPLES = """
# Install the latest JDK.
- java: state=jdk
"""

#############################################################################
#############################################################################

import os
import re
import platform
import tempfile
import traceback
import stat
import shutil
from collections import namedtuple

#############################################################################
# Utilities
#############################################################################

class PackageManager(object):
    pass

#############################################################################
#############################################################################

class Yum(PackageManager):
    
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
        argv = ['yum', '--nogpgcheck', '-y', 'install', name]
        self.module.run_command(argv, True)
        return True
        
    def uninstall(self, name):
        if not self.installed(name):
            return False
        argv = ['yum', '-y', 'remove', name]
        self.module.run_command(argv, True)
        return True
    
#############################################################################
#############################################################################

# mostly borrowed from ansible apt module code
class Apt(PackageManager):
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
    
    # I'm not sure that this function works as intended...
    def update(self):
        argv = self.args()
        argv.extend(['-q', '-y'])
        argv.append('update')
        result = self.module.run_command(' '.join(argv), True)
        return result
    
#############################################################################
#############################################################################

class AptKey(object):
    CMD = 'apt-key'
    PATH = '/usr/bin'
    
    @classmethod
    def installed(cls, module, key):
        args = "%s list | grep '%s'" % (os.path.join(cls.PATH, cls.CMD), key)
        result = module.run_command(args)
        return len(result[1])
    
    @classmethod
    def install(cls, module, key):
        if cls.installed(module, key):
            return False
        argv = [os.path.join(cls.PATH, cls.CMD), 'adv',
                '--keyserver', 'keys.gnupg.net', '--recv-keys', key]
        module.run_command(argv, True)
        return True
    
    @classmethod
    def uninstall(cls, module, key):
        if not cls.installed(module, key):
            return False
        argv = [os.path.join(cls.PATH, cls.CMD), 'del', key]
        module.run_command(argv, True)
        return True
        
#############################################################################
#############################################################################

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
        args = "grep -E -v '^#|^ *$' /etc/apt/sources.list /etc/apt/sources.list.d/*.list"
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
    
    VERSION_TEMPLATE = r'1.%(major)d.%(minor)d_%(release)d'
    UPDATE_TEMPLATE = r'%(major)du%(release)d'
    BUILD_TEMPLATE = UPDATE_TEMPLATE + '-b%(build)02d'
    
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
            fields[k] = int(v) if v is not None else 0
        return cls(**fields)
    
    def __new__(cls, major=0, minor=0, release=0, build=0):
        return super(JavaVersion, cls).__new__(cls, major, minor, release, build)
    
    def version_string(self):
        return self.VERSION_TEMPLATE % self._asdict()
    
    def update_string(self):
        return self.UPDATE_TEMPLATE % self._asdict()
    
    def build_string(self):
        return self.BUILD_TEMPLATE % self._asdict()

#############################################################################
#############################################################################

class JavaEnv(object):

    ENV_VAR = 'JAVA_HOME'
    ENV_PATTERN = r"^[:space]*JAVA_HOME[:space]*="
    
    @classmethod    
    def install(cls, module, distro, home):
        changed = False
        
        # add home to system env file
        do_append = True
        pattern = cls.ENV_PATTERN
        argv = ['grep', '-E', "%s" % pattern, distro.ENV_FILE]
        result = module.run_command(argv)
        if result[0] == 0:
            output = [l for l in result[1].splitlines(True) if '=' in l]
            assert len(output), output
            # assume that the latest value wins
            kv = [w.strip() for w in output[-1].strip().split('=')]
            assert kv[0] == cls.ENV_VAR, kv
            if kv[1].strip('"') == home:
                # home already set to requested value
                do_append = False
            else:
                # delete existing JAVA_HOME
                # it would be better to comment out, but unsure of file format
                lines = []
                with open(distro.ENV_FILE, 'r') as f:
                    for line in f:
                        if line not in output:
                            lines.append(line)
                with open(distro.ENV_FILE, 'w') as f:
                    f.writelines(lines)
                changed = True
        # append new value
        if do_append:
            with open(distro.ENV_FILE, 'a') as f:
                f.write('%s="%s"\n' % (cls.ENV_VAR, home))
            changed = True
        
        # update system alternatives
        cmd = distro.ALTERNATIVES_CMD
        for prog in ('java', 'javac',):
            source = os.path.join(home, 'bin', prog)
            if not os.path.exists(source):
                continue
            dest = os.path.join('/usr/bin', prog)
            argv = [cmd, '--install', dest, prog, source, '1',]
            module.run_command(argv, True)
            argv = [cmd, '--set', prog, source,]
            module.run_command(argv, True)
            changed = True
        
        return changed
    
    @classmethod 
    def uninstall(cls, module, distro, home=''):
        changed = False
        
        # remove home from system env file
        pattern = cls.ENV_PATTERN
        if home:
            pattern += r'[:space]*"?' + home + r'"?[:space]*$'
        argv = ['grep', '-E', "%s" % pattern, distro.ENV_FILE]
        result = module.run_command(argv)
        if result[0] == 0:
            output = [l for l in result[1].splitlines(True) if '=' in l]
            assert len(output), output
            lines = []
            with open(distro.ENV_FILE, 'r') as f:
                for line in f:
                    if line not in output:
                        lines.append(line)
            with open(distro.ENV_FILE, 'w') as f:
                f.writelines(lines)
            changed = True
        
        # update system alternatives
        cmd = distro.ALTERNATIVES_CMD
        # alternatives doesn't have --list
        for prog in ('java', 'javac',):
            args = ' '.join([cmd, '--display', prog,]) + " | grep -E '^/'"
            result = module.run_command(args)
            if result[0] == 0:
                for line in result[1].splitlines():
                    path = line.split()[0]
                    if path.startswith(home):
                        argv = [cmd, '--remove', prog, path,]
                        module.run_command(argv, True)
                        changed = True
        
        return changed
        
#############################################################################
#############################################################################

class Java(object):

    # see https://forums.oracle.com/forums/thread.jspa?messageID=10563534
    ORACLE_COOKIE = 'Cookie: gpw_e24=http%3A%2F%2Fwww.oracle.com%2F'
    ORACLE_DOWNLOAD_URL = 'http://download.oracle.com/otn-pub/java/jdk/'
    SUN_DOWNLOAD_URL = 'http://javadl.sun.com/webapps/download/AutoDL?BundleId='
    SUN_DOWNLOAD_IDS = {
        7: { 'x64': {'rpm': 80804, 'bin': 80805,}, 
             'i586': {'rpm': 80802, 'bin': 80803,},},
    }
    ORACLE_FILE_PATTERN = r'^(\w+)-(\w+)-linux-(\w+)((?:\.|-).+)$'
    ORACLE_FILE_TEMPLATE = '%s-%s-linux-%s%s'
    
    # TODO: get latest versions dynamically
    LATEST_VERSION = { 
        7: JavaVersion(7, 0, 40, 43),
    }
    
    JAVA_HOME = '/usr/lib/jvm'
    
    arguments = {
        'state': {'default': 'jre', 'choices': ['none', 'jre', 'jdk',],},
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
        return 'x64' if platform.machine() == 'x86_64' else 'i586'
    
    @classmethod
    def sun_url(cls, version, rpm=False):
        version = version.major
        if version not in cls.SUN_DOWNLOAD_IDS:
            raise NotImplementedError
        arch = cls.discover_arch()
        suffix = 'rpm' if rpm else 'bin'
        bundleid = cls.SUN_DOWNLOAD_IDS[version][arch][suffix]
        url = cls.SUN_DOWNLOAD_URL + str(bundleid)
        return url
    
    @classmethod
    def oracle_file(cls, version, jdk=False, rpm=False):
        arch = cls.discover_arch()
        prefix = 'jdk' if jdk else 'jre'
        suffix = '.rpm' if rpm else '.tar.gz'
        filename = cls.ORACLE_FILE_TEMPLATE \
            % (prefix, version.update_string(), arch, suffix)
        return filename
        
    @classmethod
    def oracle_url(cls, version, jdk=False, rpm=False):
        url = cls.ORACLE_DOWNLOAD_URL \
            + version.build_string() \
            + '/' + cls.oracle_file(version, jdk, rpm)
        return url
    
    @classmethod
    def url(cls, version, jdk, rpm):
        # for JRE, prefer sun url
        return cls.oracle_url(version, jdk, rpm) \
            if jdk else cls.sun_url(version, rpm)
    
    @classmethod
    def java_home(cls, version, jdk=False):
        latest = cls.LATEST_VERSION[version.major]
        return os.path.join(cls.JAVA_HOME, 
                            ('jdk' if jdk else 'jre') + latest.version_string())
            
    @classmethod
    def fetch_package(cls, module, distro, version, jdk=False, rpm=False, destdir=None):
        filename = cls.oracle_file(version, jdk, rpm)
        
        # use custom location if specified
        source = module.params['package_location']
        if source:
            if source.startswith('/'):
                if not os.path.exists(source):
                    raise ValueError("Non-existent path: %s" % source)
                if os.path.isdir(source) and not source.endswith('/'):
                    source += '/'
            if source.endswith('/'):
                source += filename
                if not os.path.exists(source):
                    raise ValueError("Non-existent package: %s" % source)
        else:
            source = cls.url(version, jdk, rpm)
        
        dest = None
        if source.startswith('/'): # assume local file
            dest = source
        else: # assume url
            if source.startswith(cls.ORACLE_DOWNLOAD_URL):
                opts = ('-c', '--no-cookies', '--header', cls.ORACLE_COOKIE,)
            else:
                opts = None
            dest = distro.download(module, source, opts=opts, destfile=filename, destdir=destdir)
        assert os.path.exists(dest), dest
        return dest
    
    @classmethod
    def extract_package(cls, module, distro, source, destdir=None):
        assert os.path.exists(source), source
        sourcedir, sourcefile = os.path.split(source)
        m = re.match(cls.ORACLE_FILE_PATTERN, sourcefile)
        if m is None:
            # assume already extracted
            return source
        state, version, arch, suffix = m.groups()
        version = JavaVersion.from_string(version)
        if suffix in ('.tar.gz', '.bin'):
            destfile = state + version.version_string()
        else:
            # assume already extracted
            return source
        if destdir is None:
            destdir = sourcedir
        if not os.path.isdir(destdir):
            raise RuntimeError(destdir)
        dest = os.path.join(destdir, destfile)
        if not os.path.exists(dest):
            cwd = os.getcwd()
            os.chdir(destdir)
            if suffix.endswith('.bin'):
                o755 = stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH
                os.chmod(source, o755)
                argv = [source]
                module.run_command(argv, True)
            elif suffix == '.tar.gz':
                argv = ['tar', 'xzf', source]
                module.run_command(argv, True)
            else:
                assert False, suffix
            os.chdir(cwd)
        assert os.path.exists(dest), dest
        return dest
            
    @classmethod
    def main(cls, module, *args, **kwargs):
        distro = Distribution.discover(module)
        subcls = distro.Java
        self = subcls(module, distro, *args, **kwargs)
        return self.apply()

    def __init__(self, module, distro):
        self.module = module
        self.distro = distro
        self.packages = distro.PackageManager(module)

    def install(self, state, version, rpm=False):
        module = self.module
        distro = self.distro
        jdk = state=='jdk'
        changed = False
        
        # fetch and extract source
        destdir = self.JAVA_HOME
        if not os.path.isdir(destdir):
            o755 = stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH
            os.makedirs(destdir, o755)
            changed = True
        source = self.fetch_package(module, distro, version, jdk, rpm, destdir)
        dest = self.extract_package(module, distro, source, destdir)
        if dest != source:
            changed = True
        
        # install package
        if rpm:
            changed = self.packages.install(dest) or changed
        
        # update env
        home = self.java_home(version, jdk)
        changed = JavaEnv.install(module, distro, home) or changed
        
        return changed
    
    def uninstall(self, purge=False):
        module = self.module
        distro = self.distro
        changed = False
        if purge:
            home = self.JAVA_HOME
            if os.path.exists(home):
                shutil.rmtree(home)
                changed = True
        changed = JavaEnv.uninstall(module, distro) or changed
        return changed
    
    def apply(self):
        module = self.module

        current_state = 'none'
        current_version = self.discover_version(module, True)
        if current_version:
            current_state = 'jdk'
        else:
            current_version = self.discover_version(module, False)
            if current_version:
                current_state = 'jre'
        
        result = {
            'changed': False,
            'state': current_state,
            'version': '',
            'java_home': '',
        }
        
        if current_version:
            result['version'] = current_version.version_string()
            result['java_home'] = self.java_home(current_version, current_state == 'jdk')
        
        target_state = module.params['state']
        if target_state == 'none':
            target_version = None
        else:
            target_version = JavaVersion(7, 0, 0, 0)
        
        # are we done?
        if current_state == target_state:
            # check version
            if current_state != 'none':
                if current_version >= target_version:
                    return result
            else:
                return result
        
        # short circuit for check mode
        if module.check_mode:
            result['changed'] = True
            current_version = target_version
        else:
            # uninstall existing java
            if current_state != 'none':
                result['changed'] = self.uninstall() or result['changed']
                current_version = self.discover_version(module, current_state == 'jdk')
                assert not current_version, current_version
                        
            if target_state != 'none':
                # bump target version up to latest version
                if target_version.major not in self.LATEST_VERSION:
                    raise NotImplementedError
                result['changed'] = self.install(target_state, self.LATEST_VERSION[target_version.major]) or result['changed']
                current_version = self.discover_version(module, target_state == 'jdk')
                assert current_version and current_version >= target_version, current_version
        
        result['state'] = target_state
        result['version'] = current_version.version_string() if current_version else ''
        result['java_home'] = self.java_home(current_version, target_state == 'jdk') if current_version else ''
        
        return result

#############################################################################
#############################################################################

# lg: For JRE 6, I tried to use https://github.com/flexiondotorg/oab-java6
# but I ran into problems with ia32-libs on my test machine
# so for now, JRE 6 install is manual
class JavaDeb(Java):
    
    # For JDK 6/7
    JDK_REPO = 'ppa:webupd8team/java'

    # FOR JRE 7
    JRE_REPO = 'deb http://www.duinsoft.nl/pkg debs all'
    JRE_REPO_KEY = '5CB26B26'
    JRE_REPO_FILE = '/etc/apt/sources.list.d/duinsoft.list'
    
    @classmethod
    def java_home(cls, version, jdk=False):
        if jdk:
            return os.path.join(cls.JAVA_HOME, 
                                'java-%d-oracle' % version.major)
        else:
            return super(JavaDeb, cls).java_home(version, jdk)
    
    @classmethod
    def java_package(cls, version, jdk=True):
        if jdk:
            name = 'oracle-java%d-installer' % version.major
        else:
            return 'update-sun-jre'
        return name

    def install_jdk(self, version):
        changed = False
        repo = self.JDK_REPO
        aptrepo = AptRepository(self.module)
        changed = aptrepo.install(repo) or changed
        pkg = self.java_package(version, True)
        if not self.packages.installed(pkg):
            # accept Oracle license
            args = " | ".join(("echo %s shared/accepted-oracle-license-v1-1 select true" % pkg,
                               "/usr/bin/debconf-set-selections"))
            self.module.run_command(args, True)
        changed = self.packages.install(pkg) or changed
        return changed
    
    def uninstall_jdk(self):
        changed = False
        for version in self.arguments['version']['choices']:
            pkg = self.java_package(JavaVersion.from_string(version), True)
            changed = self.packages.uninstall(pkg) or changed
        repo = self.JDK_REPO
        aptrepo = AptRepository(self.module)
        changed = aptrepo.uninstall(repo) or changed
        return changed
        
    def install_jre(self, version):
        changed = False
        if version.major == 7:
            if not os.path.isfile(self.JRE_REPO_FILE):
                with open(self.JRE_REPO_FILE, 'w') as f:
                    f.write(self.JRE_REPO)
                    f.write('\n')
                changed = True

            changed = AptKey.install(self.module, self.JRE_REPO_KEY) or changed
            if changed:
                self.packages.update()
            
            pkg = self.java_package(version, False)
            changed = self.packages.install(pkg) or changed
        else:
            raise NotImplementedError
        return changed
        
    def uninstall_jre(self):
        changed = False
        for version in self.arguments['version']['choices']:
            pkg = self.java_package(JavaVersion.from_string(version), False)
            changed = self.packages.uninstall(pkg) or changed
        
        if os.path.isfile(self.JRE_REPO_FILE):
            os.remove(self.JRE_REPO_FILE)
            changed = True
            self.packages.update()

        changed = AptKey.uninstall(self.module, self.JRE_REPO_KEY) or changed
        changed = super(JavaDeb, self).uninstall() or changed
        return changed
    
    def install(self, state, version):
        module = self.module
        distro = self.distro
        changed = False
        
        self.packages.update()
        jdk = state == 'jdk'
        if jdk:
            changed = self.install_jdk(version) or changed
        else:
            changed = self.install_jre(version) or changed
        home = self.java_home(version, jdk)
        changed = JavaEnv.install(module, distro, home) or changed
        return changed
        
    def uninstall(self):
        module = self.module
        distro = self.distro
        changed = False
        changed = self.uninstall_jdk() or changed
        changed = self.uninstall_jre() or changed
        changed = JavaEnv.uninstall(module, distro) or changed
        return changed
    
#############################################################################
#############################################################################

class JavaRhel(Java):
    # For later:
    # http://www.rackspace.com/knowledge_center/article/how-to-install-the-oracle-jdk-on-fedora-15-16
    # https://github.com/p120ph37/java-1.7.0-sun-compat
    
    JAVA_HOME = '/usr/java'
    
    @classmethod
    def java_home(cls, version=None, jdk=False):
        return os.path.join(cls.JAVA_HOME, 'default')

    def install(self, state, version):
        return super(JavaRhel, self).install(state, version, True)
        
    def uninstall(self):
        changed = False
        pkgs = ['jdk', 'jre']
        for pkg in pkgs:
            changed = self.packages.uninstall(pkg) or changed
        changed = JavaEnv.uninstall(self.module, self.distro) or changed
        return changed

#############################################################################
# Distribution details
#############################################################################

class Distribution(object):
    ENV_FILE = '/etc/environment'
    DOWNLOAD_CMD = 'wget'
    
    Java = Java
    supported = {}
    
    @classmethod
    def discover(cls, module): 
        dist = get_distribution() # module_common
        if not dist:
            raise RuntimeError('Platform not supported: %s' % get_platform()) # module_common
        subcls = None
        for dists, subcls in cls.supported.iteritems():
            if dist in dists:
                break
        else:
            raise RuntimeError('Distribution not supported: %s' % dist)
        return subcls

    @classmethod
    def download(cls, module, source, opts=None, destfile=None, destdir=None):
        if destdir is None:
            destdir = tempfile.gettempdir()
        if destfile is None:
            destfile = source.rsplit('/', 1)[1]
        dest = os.path.join(destdir, destfile)
        
        if opts is None:
            opts = ('-c', '--no-cookies')
        
        argv = [cls.DOWNLOAD_CMD]
        argv.extend(opts)
        argv.append(source)
        argv.append('--output-document=%s' % dest)
        result = module.run_command(argv)
        if result[0] != 0:
            raise RuntimeError('Error: Download returned %d: %s' % (result[0], argv))
        
        return dest

class DebDistribution(Distribution):
    ALTERNATIVES_CMD = 'update-alternatives'
    PackageManager = Apt
    Java = JavaDeb
super(DebDistribution, DebDistribution).supported[('Ubuntu',)] = DebDistribution

class RhelDistribution(Distribution):
    ALTERNATIVES_CMD = 'alternatives'
    PackageManager = Yum
    Java = JavaRhel
super(RhelDistribution, RhelDistribution).supported[('Fedora',)] = RhelDistribution

#############################################################################
#############################################################################

def main():
    mod = AnsibleModule(argument_spec=Java.arguments,
                        supports_check_mode=True) # module_common
    try:
        result = Java.main(mod)
    except Exception:
        msg = traceback.format_exc()
        mod.fail_json(msg=msg)
    else:
        mod.exit_json(**result)

#############################################################################
#############################################################################

# include magic from lib/ansible/module_common.py
#<<INCLUDE_ANSIBLE_MODULE_COMMON>>
main()
