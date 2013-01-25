#!/usr/bin/python

DOCUMENTATION = """
---
module: java
author: Lisa Glendenning
short_description: Manages installation of Oracle Java 6/7 on Linux
description:
    - Manage installation of Oracle Java 6/7 on Linux.
requirements:
    - wget
#version_added: null
notes:
    - "Tested with Ansible v0.9."
    - "Tested on 64-bit Fedora 15,18 and Ubuntu 12."
    - "Undefined behavior if mixed with other Java installations."
options:
    state:
        description:
            - "whether to install JRE (I(jre)), install JDK (I(jdk)), or uninstall (I(none))"
        required: false
        default: jre
        choices: [none, jre, jdk]
    version:
        description:
            - "Java version"
        required: false
        default: 7
        choices: [6, 7]
    package_location:
        description:
            - "non-standard location for packages"
        required: false
        default: None
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
import stat
import shutil
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
            if v is not None:
                fields[k] = int(v)
        return cls(**fields)
    
    def __new__(cls, major=None, minor=None, release=None, build=None):
        return super(JavaVersion, cls).__new__(cls, major, minor, release, build)
    
    def version_string(self):
        return self.VERSION_TEMPLATE % self._asdict()
    
    def update_string(self):
        return self.UPDATE_TEMPLATE % self._asdict()
    
    def build_string(self):
        return self.BUILD_TEMPLATE % self._asdict()

#############################################################################
#############################################################################

class Java(object):
    
    # see https://forums.oracle.com/forums/thread.jspa?messageID=10563534
    ORACLE_COOKIE = 'Cookie: gpw_e24=http%3A%2F%2Fwww.oracle.com%2F'
    ORACLE_DOWNLOAD_URL = 'http://download.oracle.com/otn-pub/java/jdk/'
    SUN_DOWNLOAD_URL = 'http://javadl.sun.com/webapps/download/AutoDL?BundleId='
    SUN_DOWNLOAD_IDS = {
        7: { 'x64': {'rpm': 73133, 'bin': 73134,}, 
             'i586': {'rpm': 73131, 'bin': 73132,},},
        6: { 'x64': {'rpm': 71304, 'bin': 71305,}, 
             'i586': {'rpm': 71302, 'bin': 71303,},},
    }
    
    JAVA_HOME = '/usr/lib/jvm'
    
    # TODO: get latest versions dynamically
    LATEST_VERSION = { 
        7: JavaVersion(7, 0, 11, 21),
        6: JavaVersion(6, 0, 38, 5),
    }
    
    subclasses = {}
    
    arguments = {
        'state': {'default': 'jre', 'choices': ['none', 'jre', 'jdk',],},
        'version': {'default': '7', 'choices': ['6', '7',],},
        'vendor': {'default': 'Oracle', 'choices': ['Oracle',],},
        'package_location': {'default': None,}
    }
    
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
    def sun_url(cls, target_version, rpm=False):
        version = target_version.major
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
        if version.major == 7:
            suffix = '.rpm' if rpm else '.tar.gz'
        elif version.major == 6:
            suffix = '-rpm.bin' if rpm else '.bin'
        else:
            raise NotImplementedError
        filename = '%s-%s-linux-%s%s' \
            % (prefix, version.update_string(), arch, suffix)
        return filename
        
    @classmethod
    def oracle_url(cls, target_version, jdk=False, rpm=False):
        if target_version.major not in cls.LATEST_VERSION:
            raise NotImplementedError
        version = cls.LATEST_VERSION[target_version.major]
        url = cls.ORACLE_DOWNLOAD_URL \
            + version.build_string() \
            + '/' + cls.oracle_file(version, jdk, rpm)
        return url
    
    @classmethod
    def java_home(cls, version, jdk=False):
        latest = cls.LATEST_VERSION[version.major]
        return os.path.join(cls.JAVA_HOME, 
                            ('jdk' if jdk else 'jre') + latest.version_string())
    
    @classmethod
    def fetch(cls, module, target_version, jdk=False, rpm=False):
        url = cls.oracle_url(target_version, jdk, rpm)
        filename = url.rsplit('/', 1)[1]
        
        source = None
        if module.params['package_location']:
            source = module.params['package_location']
            if source.startswith('/') and not source.endswith('/'):
                source += '/'
        else:
            # for JRE, prefer sun url
            if not jdk:
                url = cls.sun_url(target_version, rpm)
            source = url
        if source.endswith('/'):
            source += filename
        
        dest = None
        if source.startswith('/'): # assume local file
            dest = source
        else: # assume url
            download = Download(module)
            if url.startswith(cls.ORACLE_DOWNLOAD_URL):
                opts = ('-c', '--no-cookies', '--header', cls.ORACLE_COOKIE,)
            else:
                opts = None
            dest = download.fetch(source, opts=opts, destfile=filename)
        return dest
        
    @classmethod
    def main(cls, module, *args, **kwargs):
        subcls = cls.select_subclass()
        self = subcls(module, *args, **kwargs)
        return self.apply()

    def __init__(self, module):
        self.module = module

    def install(self, target_state, target_version):
        module = self.module
        jdk = target_state=='jdk'
        rpm = False
        source = self.fetch(module, target_version, jdk, rpm)
        sourcedir = os.path.split(source)[0]
        
        dest = self.java_home(target_version, jdk)
        destdir, destfile = os.path.split(dest)
        o755 = stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH
        if not os.path.exists(os.path.join(sourcedir, destfile)):
            cwd = os.getcwd()
            os.chdir(sourcedir)
            if source.endswith('.bin'):
                os.chmod(source, o755)
                argv = [source]
                module.run_command(argv, True)
            elif source.endswith('.tar.gz'):
                argv = ['tar', 'xzf', source]
                module.run_command(argv, True)
            else:
                raise NotImplementedError
            os.chdir(cwd)
        
        source = os.path.join(sourcedir, destfile)
        assert os.path.isdir(source)
        if not os.path.isdir(destdir):
            os.makedirs(destdir, o755)
        if not os.path.exists(dest):
            shutil.move(source, dest)
        for cmd in (['java'] + (['javac'] if target_state == 'jdk' else [])):
            argv = ['update-alternatives', '--install', 
                    '/usr/bin/%s' % cmd, cmd,
                    os.path.join(dest, 'bin', cmd), '1']
            module.run_command(argv, True)
            argv = ['update-alternatives', '--set', cmd,
                    os.path.join(dest, 'bin', cmd),]
            module.run_command(argv, True)
            
        return True
    
    def uninstall(self, purge=False):
        changed = False
        module = self.module
        dest = self.JAVA_HOME
        if purge:
            if os.path.exists(dest):
                shutil.rmtree(dest)
                changed = True
            
        for cmd in ('java', 'javac',):
            argv = ['update-alternatives', '--list', cmd,]
            result = module.run_command(argv)
            if result[0] == 0:
                for path in result[1].splitlines():
                    if path.startswith(dest):
                        argv = ['update-alternatives', '--remove', cmd, path,]
                        module.run_command(argv, True)
                        changed = True

        return changed
    
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
#            current_version = self.discover_version(module, current_state == 'jdk')
#            assert not current_version, current_version
        
        if target_state != 'none':
            result['changed'] = bool(self.install(target_state, target_version)) or result['changed']
            current_version = self.discover_version(module, target_state == 'jdk')
            assert current_version and current_version >= target_version, current_version
        
        return result

#############################################################################
#############################################################################

# For JRE 6, I tried to use https://github.com/flexiondotorg/oab-java6
# but I ran into problems with ia32-libs on my test machine
# so for now, JRE 6 install is manual
class JavaDeb(Java):
    
    # For JDK 6/7
    JDK_REPO = 'ppa:webupd8team/java'

    # FOR JRE 7
    JRE_REPO = 'deb http://www.duinsoft.nl/pkg debs all'
    JRE_REPO_KEY = '5CB26B26'
    JRE_REPO_FILE = '/etc/apt/sources.list.d/duinsoft.list'
    
    ENV_FILE = '/etc/environment'
    ENV_PATTERN = r"^[:space]*JAVA_HOME[:space]*="
    
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
        
    def __init__(self, module):
        super(JavaDeb, self).__init__(module)
        self.apt = Apt(module)
        self.aptrepo = AptRepository(module)
            
    def install_env(self, home):
        pattern = self.ENV_PATTERN
        argv = ['grep', '-E', "%s" % pattern, self.ENV_FILE]
        result = self.module.run_command(argv)
        if result[0] == 0:
            output = [l for l in result[1].splitlines(True) if '=' in l]
            assert len(output)
            # assume that the latest value wins
            kv = [w.strip() for w in output[-1].strip().split('=')]
            assert kv[0] == 'JAVA_HOME'
            if kv[1].strip('"') == home:
                return False
            
            # delete existing JAVA_HOME
            # it would be better to comment out, but unsure of file format
            lines = []
            with open(self.ENV_FILE, 'r') as f:
                for line in f:
                    if line not in output:
                        lines.append(line)
            
            with open(self.ENV_FILE, 'w') as f:
                f.writelines(lines)
                
        # append
        with open('/etc/environment', 'a') as f:
            f.write('JAVA_HOME="%s"\n' % home)
        return True
    
    def uninstall_env(self, home=''):
        pattern = self.ENV_PATTERN
        if home:
            pattern += r'[:space]*"?' + home + r'"?[:space]*$'
        argv = ['grep', '-E', "%s" % pattern, self.ENV_FILE]
        result = self.module.run_command(argv)
        if result[0] == 0:
            output = [l for l in result[1].splitlines(True) if '=' in l]
            assert len(output)
            lines = []
            with open(self.ENV_FILE, 'r') as f:
                for line in f:
                    if line not in output:
                        lines.append(line)
            with open(self.ENV_FILE, 'w') as f:
                f.writelines(lines)
            return True
        else:
            return False
        
    def install_jdk(self, target_version):
        changed = False
        repo = self.JDK_REPO
        changed = self.aptrepo.install(repo) or changed
        pkg = self.java_package(target_version, True)
        if not self.apt.installed(pkg):
            # accept Oracle license
            args = " | ".join(("echo %s shared/accepted-oracle-license-v1-1 select true" % pkg,
                               "/usr/bin/debconf-set-selections"))
            self.module.run_command(args, True)
        changed = self.apt.install(pkg) or changed
        return changed
    
    def uninstall_jdk(self):
        changed = False
        for version in self.arguments['version']['choices']:
            pkg = self.java_package(JavaVersion.from_string(version), True)
            changed = self.apt.uninstall(pkg) or changed
        repo = self.JDK_REPO
        changed = self.aptrepo.uninstall(repo) or changed
        return changed
        
    def install_jre(self, target_version):
        changed = False
        if target_version.major == 7:
            if not os.path.isfile(self.JRE_REPO_FILE):
                with open(self.JRE_REPO_FILE, 'w') as f:
                    f.write(self.JRE_REPO)
                    f.write('\n')
                changed = True
        
            aptkey = AptKey(self.module)
            changed = aptkey.install(self.JRE_REPO_KEY) or changed
            if changed:
                self.apt.update()
            
            pkg = self.java_package(target_version, False)
            changed = self.apt.install(pkg) or changed
        elif target_version.major == 6:
            changed = super(JavaDeb, self).install('jre', target_version)
        else:
            raise NotImplementedError
        return changed
        
    def uninstall_jre(self):
        changed = False
        for version in self.arguments['version']['choices']:
            pkg = self.java_package(JavaVersion.from_string(version), False)
            changed = self.apt.uninstall(pkg) or changed
        
        if os.path.isfile(self.JRE_REPO_FILE):
            os.remove(self.JRE_REPO_FILE)
            changed = True
            self.apt.update()
            
        aptkey = AptKey(self.module)
        changed = aptkey.uninstall(self.JRE_REPO_KEY) or changed
        
        changed = super(JavaDeb, self).uninstall() or changed

        return changed
    
    def install(self, target_state, target_version):
        changed = False
        self.apt.update()
        if target_state == 'jdk':
            changed = self.install_jdk(target_version) or changed
            jdk = True
        elif target_state == 'jre':
            changed = self.install_jre(target_version) or changed
            jdk = False
        else:
            raise ValueError(target_state)
        home = self.java_home(target_version, jdk)
        changed = self.install_env(home) or changed
        return changed
        
    def uninstall(self):
        changed = False
        changed = self.uninstall_jdk() or changed
        changed = self.uninstall_jre() or changed
        changed = self.uninstall_env() or changed
        return changed
    
super(JavaDeb, JavaDeb).subclasses[('Ubuntu',)] = JavaDeb

#############################################################################
#############################################################################

class JavaRhel(Java):
    # For later:
    # http://www.rackspace.com/knowledge_center/article/how-to-install-the-oracle-jdk-on-fedora-15-16
    # https://github.com/p120ph37/java-1.7.0-sun-compat
    
    def __init__(self, module):
        super(JavaRhel, self).__init__(module)
        self.yum = Yum(module)

    def install(self, target_state, target_version):
        module = self.module
        jdk = target_state == 'jdk'
        rpm = True
        source = self.fetch(self.module, target_version, jdk, rpm)
        if source.endswith('.bin'):
            sourcedir, sourcefile = os.path.split(source)
            # just to be difficult, the 64bit -rpm.bin from java.com
            # turns into a file with amd64 in the name
            if target_version.major == 6 and self.discover_arch() == 'x64':
                destfile = sourcefile.replace('-x64-rpm.bin', '-amd64.rpm')
            else:
                destfile = sourcefile.replace('-rpm.bin', '.rpm')
            dest = os.path.join(sourcedir, destfile)
            if not os.path.exists(dest): 
                o755 = stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH
                os.chmod(source, o755)
                cwd = os.getcwd()  
                os.chdir(sourcedir)
                argv = [source]
                module.run_command(argv, True)
                os.chdir(cwd)
                assert os.path.exists(dest)
            source = dest
        changed = self.yum.install(source)
        return changed
        
    def uninstall(self):
        changed = False
        pkgs = ['jdk', 'jre']
        for pkg in pkgs:
            changed = self.yum.uninstall(pkg) or changed
        return changed
    
super(JavaRhel, JavaRhel).subclasses[('Fedora',)] = JavaRhel

#############################################################################
#############################################################################

# because Ansible 0.9 doesn't include this function
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
