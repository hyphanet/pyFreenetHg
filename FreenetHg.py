#!/usr/bin/env python
# -*- coding: utf-8 -*-
# This program is free software. It comes without any warranty, to
# the extent permitted by applicable law. You can redistribute it
# and/or modify it under the terms of the Do What The Fuck You Want
# To Public License, Version 2, as published by Sam Hocevar. See
# http://sam.zoy.org/wtfpl/COPYING for more details. */

import os
import time
import tempfile
import random
import socket
import sys
import dircache

from nntplib import NNTP
from StringIO import StringIO
from string import Template

from mercurial import hg
from mercurial import commands
from mercurial import repo, cmdutil, util, ui, revlog, node
from mercurial.node import bin

#
# fcp rape begin
#

# the stuff below is treated as lib, so it should not refer to hg or other non-python-builtin stuff

REQUIRED_NODE_VERSION=1183
REQUIRED_EXT_VERSION=26

DEFAULT_FCP_HOST = "127.0.0.1"
DEFAULT_FCP_PORT = 9481
DEFAULT_TIMEOUT = 300

# utils
def _getUniqueId():
    """Allocate a unique ID for a request"""
    timenum = int( time.time() * 1000000 );
    randnum = random.randint( 0, timenum );
    return "id" + str( timenum + randnum );

# asynchronous stuff (single thread)
class FCPIOConnection(object):
    """class for real i/o and format helpers"""
    
    def __init__(self, host, port, timeout):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.settimeout(timeout)
        try:
            self.socket.connect((host, port))
        except Exception, e:
            raise Exception("Failed to connect to %s:%s - %s" % (host, port, e))
        pass
    
    def __del__(self):
        """object is getting cleaned up, so disconnect"""
        try:
            self.socket.close()
        except:
            pass
        
    def _readline(self):
        buf = []
        while True:
            c = self.socket.recv(1)
            if c == '\n':
                break
            buf.append(c)
        ln = "".join(buf)
        return ln
    
    def read(self, n):
        chunks = []
        remaining = n
        while remaining > 0:
            chunk = self.socket.recv(remaining)
            chunklen = len(chunk)
            if chunk:
                chunks.append(chunk)
            else:
                raise Exception("FCP socket closed by node")
            remaining -= chunklen
        buf = "".join(chunks)
#        print "testi: in:<"+str(len(buf))+" Bytes of data>" 
        return buf

    def readEndMessage(self):
        #the first line is the message name
        messagename = self._readline()
#        print "testi: in:"+messagename
        items = {}
        while True:
            line = self._readline();
#            print "testi: in:"+line
            if (len(line.strip()) == 0):
                continue # an empty line, jump over
            
            if line in ['End', 'EndMessage', 'Data']:
                endmarker = line
                break
            
            # normal 'key=val' pairs left
            k, v = line.split("=", 1)
            items[k] = v
             
        return FCPMessage(messagename, items, endmarker)
    
    def _sendLine(self, line):
#        print "testi: out:"+line
        self.socket.sendall(line+"\n")
    
    def _sendMessage(self, messagename, hasdata=False, **kw):
        self._sendLine(messagename)
        for k, v in kw.items():
            line = k + "=" + str(v)
            self._sendLine(line)
        if kw.has_key("DataLength") or hasdata:
            self._sendLine("Data")
        else:
            self._sendLine("EndMessage")
    
    def _sendCommand(self, messagename, hasdata, kw):
        self._sendLine(messagename)
        for k, v in kw.items():
            line = k + "=" + str(v)
            self._sendLine(line)
        if kw.has_key("DataLength") or hasdata:
            self._sendLine("Data")
        else:
            self._sendLine("EndMessage")
    
    def _sendData(self, data):
#        print "testi: out:<"+str(len(data))+" Bytes of data>" 
        self.socket.sendall(data)

class FCPConnection(FCPIOConnection):
    """class for low level fcp protocol i/o"""
    
    def __init__(self, host, port, timeout):
        """c'tor leaves a ready to use connection (hello done)"""
        FCPIOConnection.__init__(self, host, port, timeout)
        self._helo()
        
    def _helo(self):
        """perform the initial FCP protocol handshake"""
        name = _getUniqueId()
        self._sendMessage("ClientHello", Name=name, ExpectedVersion="2.0")
        msg = self.readEndMessage()
        if not msg.isMessageName("NodeHello"):
            raise Exception("Node helo failed: %s" % (msg.getMessageName()))
        
        # check versions
        version = msg.getIntValue("Build")
        if version < REQUIRED_NODE_VERSION:
            raise Exception("Node to old. Found %d, but need %d" % (version, REQUIRED_NODE_VERSION))
        extversion = msg.getIntValue("ExtBuild")
        if extversion < REQUIRED_EXT_VERSION:
            raise Exception("Node-ext to old. Found %d, but need %d" % (extversion, REQUIRED_EXT_VERSION))
    
    def sendCommand(self, command, data=None):
        if data is None:
            hasdata = command.hasData()
        else:
            hasdata = True
        self._sendCommand(command.getCommandName(), hasdata, command.getItems())
        if data is not None:
            self._sendData(data)
        
    def write(self, data):
        self._sendData(data)
            
class FCPCommand(object):
    """class for client to node messages"""
    
    _items = {}
    
    def __init__(self, name, identifier=None):
        self._name = name
        if None == identifier:
            self._items['Identifier'] = _getUniqueId()
        else:
            self._items['Identifier'] = identifier

    def getCommandName(self):
        return self._name;
    
    def getItems(self):
        return self._items;
    
    def setItem(self, name, value):
        self._items[name] = value;
    
    def hasData(self):
        if self._items.has_key("DataLength"):
            return True
        else:
            return False 
            
class FCPMessage(object):
    """class for node to client messages"""
    _items = {}
    _messagename = ""
    _endmarker = ""
    
    def __init__(self, messagename, items, endmarker):
        self._messagename = messagename
        self._endmarker = endmarker
        self._items = items 
    
    def isMessageName(self, testname):
        if self._messagename in testname:
            return True
        else:
            return False
        
    def getMessageName(self):
        return self._messagename
        
    def getIntValue(self, name):
        return int(self._items[name])
    
    def getValue(self, name):
        return self._items[name]

# asynchronous stuff (thread save)
class FCPJob(object):
    """abstract class for asynchronous jobs, they may use more then one fcp command and/or interact with the node in a complex manner"""

class FCPSession(object):
    """class for managing/running FCPJobs"""
    
# the stuff above is treated as lib, so it should not refer to hg or other non-python-builtin stuff

class HgFCPConnection(FCPConnection):
    
    def __init__(self, ui, **opts):
        # defaults
        host = DEFAULT_FCP_HOST
        port = DEFAULT_FCP_PORT 
        timeout = DEFAULT_TIMEOUT
        
        host = ui.config('freenethg', 'fcphost')
        port = ui.config('freenethg', 'fcpport')
        
        #host = opts.get('host', env.get("FCP_HOST", DEFAULT_FCP_HOST))
        #port = opts.get('port', env.get("FCP_PORT", DEFAULT_FCP_PORT))
        
        # command line overwrites
        if opts.get('fcphost'):
            host = opts['fcphost']
        if opts.get('fcpport'):
            port = opts['fcpport']
        if opts.get('fcptimeout'):
            timeout = opts['fcptimeout']
        
        FCPConnection.__init__(self, host, int(port), timeout)
        
def hgBundlePut(connection, data):
    
    putcmd = FCPCommand('ClientPut')
    putcmd.setItem('Verbosity', -1)
    putcmd.setItem('URI', "CHK@")
    putcmd.setItem('MaxRetries', -1)
    putcmd.setItem('Metadata.ContentType', 'mercurial/bundle')
    putcmd.setItem('DontCompress', 'false')
    putcmd.setItem('PriorityClass', '1')
    putcmd.setItem('UploadFrom', 'direct')
    putcmd.setItem('DataLength', len(data))
    
    connection.sendCommand(putcmd, data)

    while True:
        msg = connection.readEndMessage()
        
        if msg.isMessageName('PutFetchable') or msg.isMessageName('PutSuccessful'):
            result = msg.getValue('URI')
            print "Insert Succeeded at: " + result
            return msg.getValue('URI')
        
        if msg.isMessageName('ProtocolError'):
            raise Exception("ProtocolError(%d) - %s: %s" % (msg.getIntValue('Code'), msg.getValue('CodeDescription'), msg.getValue('ExtraDescription')))
            
        if msg.isMessageName('PutFailed'):
            raise Exception("This should really not happen!")
        
        if msg.isMessageName('SimpleProgress'):
            print "Succeeded: %d  -  Required: %d  -  Total: %d  -  Failed: %d  -  Final: %s" % (msg.getIntValue('Succeeded'), msg.getIntValue('Required'), msg.getIntValue('Total'), msg.getIntValue('FatallyFailed'), msg.getValue('FinalizedTotal'))
            continue

#        print msg.getMessageName()

def hgBundleGet(connection, uri):
    
    getcmd = FCPCommand('ClientGet')
    getcmd.setItem('Verbosity', -1)
    getcmd.setItem('URI', uri)
    getcmd.setItem('MaxRetries', 5)
    getcmd.setItem('PriorityClass', '1')
    getcmd.setItem('ReturnType', 'direct')
    
    connection.sendCommand(getcmd)
    
    while True:
        msg = connection.readEndMessage()
        
        if msg.isMessageName('AllData'):
            size = msg.getIntValue('DataLength');
            return connection.read(size)
        
        if msg.isMessageName('ProtocolError'):
            raise Exception("ProtocolError(%d) - %s: %s" % (msg.getIntValue('Code'), msg.getValue('CodeDescription'), msg.getValue('ExtraDescription')))
            
        if msg.isMessageName('GetFailed'):
            raise Exception("This should really not happen!")
        
        if msg.isMessageName('SimpleProgress'):
            print "Succeeded: %d  -  Required: %d  -  Total: %d  -  Failed: %d  -  Final: %s" % (msg.getIntValue('Succeeded'), msg.getIntValue('Required'), msg.getIntValue('Total'), msg.getIntValue('FatallyFailed'), msg.getValue('FinalizedTotal'))
            continue

#        print msg.getMessageName()
            
#
# fcp rape end
#

class IndexPageMaker(object):
    """class for generate an index page"""

    def get_default_index_page(self, data):
        """generates the built-in version of index.html for repository"""

        template = Template('<!DOCTYPE html PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN" "http://www.w3.org/TR/html4/loose.dtd">\n \
                <html>\n<head>\n<meta http-equiv="Content-Type" content="text/html; charset=UTF-8">\n \
                <title>Sorry, Not a Freesite</title>\n</head>\n<body>\n \
                Sorry, this is not a freesite, this is a mercurial repository.<br>\n \
                Please use hg clone|pull static-http://127.0.0.1:8888/URI to use the repository.<br>\n \
                <p>&nbsp;</p>\n \
                created with <a href="/USK@MYLAnId-ZEyXhDGGbYOa1gOtkZZrFNTXjFl1dibLj9E,Xpu27DoAKKc8b0718E-ZteFrGqCYROe7XBBJI57pB4M,AQACAAE/pyFreenetHg/1/">pyFreenetHg</a>\n \
                </body>\n</html>\n')

        return template.substitute(data)

    def get_custom_index_page(self, ui):
        """generates the custom version of index.html for repository"""

        f = open(ui.config('freenethg', 'indextemplate'), 'r')
        template = Template(f.read())
        f.close()

        template_data = {'uri' : ui.config('freenethg', 'requesturi') or 'URI',
                         'fmsuser' : ui.config('freenethg', 'fmsuser') or '',
                         }

        page = template.substitute(template_data)
        return page

    def get_index_page(self, ui):
        """returns index.html page for repository insert.
           either the built-in version or from a user template"""

        # this dict holds the key-value-pairs for default template
        # empty at the moment
        default_data = {}

        if ui.config('freenethg', 'indextemplate'):
            try:
                page = self.get_custom_index_page(ui)
            except Exception, e:
                print "Error while processing template from %s:" % ui.config('freenethg', 'indextemplate')
                print e
                print "Using default template"
                page = self.get_default_index_page(default_data)
        else:
            page = self.get_default_index_page(default_data)

        return page

class FMS_NNTP(NNTP):
    """class for posts to newsgroups on nntp servers"""

    nntp_msg_template = Template("""From: $fms_user\nNewsgroups: $fms_groups\nSubject: $subject\nContent-Type: text/plain; charset=UTF-8\n\n$body""")

    def __init__(self, host, fms_user, groups, port=1119):
        self.fms_user = fms_user
        self.fms_groups = groups
        NNTP.__init__(self, host, port=port)

    def _load_template(self, template_path):
        user_template = None
        subject_addon = None

        try:
            f = open(template_path, 'r')
            subject_addon = f.readline().replace('\n', '').replace('\r\n', '')
            user_template = f.read()
            f.close()
        except Exception, e:
            print "Error while processing template from %s:" % template_path
            print e
            print "Using default template"

        return (subject_addon, user_template)

    def post_updatestatic(self, notify_data, template_path=None):
        uri = notify_data['uri']
        uri = uri.endswith('/') and uri[: - 1] or uri
        repository_name = uri.split('/')[1]
        repository_version = uri.split('/')[2]

        if template_path:
            subject_addon, user_template = self._load_template(template_path)
        else:
            subject_addon = user_template = None

        if user_template:
            body = Template(user_template)
        else:
            body = Template('This is an automated message of pyFreenetHg.\n\nMercurial repository update:\n$uri')

        body = body.substitute({'uri':uri})

        subject = 'Repository "%s" updated, Ed. %s' % (repository_name, repository_version)

        if subject_addon:
            subject += ' %s' % subject_addon

        template_data = {'body':body,
                         'subject':subject,
                         'fms_user':self.fms_user,
                         'fms_groups':self.fms_groups, }

        article = StringIO(self.nntp_msg_template.substitute(template_data))
        result = self.post(article)
        article.close()

        return result

    def post_bundle(self, notify_data, template_path=None):

        if template_path:
            subject_addon, user_template = self._load_template(template_path)
        else:
            subject_addon = user_template = None

        if user_template:
            body = Template(user_template)
        else:
            body = Template('This is an automated message of pyFreenetHg.\n\nBundled changeset:\nBase: $base \nRevision: $rev\nURI: $uri')

        base = ', '.join(notify_data.get('base'))
        rev = ', '.join(notify_data.get('rev'))

        result = 'Not posted (missing --base and --rev arguments)'

        if base and rev:
            body = body.substitute({'base':base,
                                    'rev':rev,
                                    'uri':notify_data.get('uri')})

            subject = 'Bundled changset for %s' % notify_data.get('repository')

            if subject_addon:
                subject += ' %s' % subject_addon

            template_data = {'body':body,
                             'subject':subject,
                             'fms_user':self.fms_user,
                             'fms_groups':self.fms_groups, }

            article = StringIO(self.nntp_msg_template.substitute(template_data))
            result = self.post(article)
            article.close()

        return result

class Notifier(object):
    """This object handles all notifications of repository updates or bundle inserts"""

    def __init__(self, ui, notify_data, autorun=False):

        self.ui = ui
        self.notify_data = notify_data

        if autorun:
            self.notify()

    def notify(self):
        uiw = self.ui.walkconfig()
        trigger = self.ui.config('freenethg', 'notify')

        if trigger:
            for section, key, value in uiw:
                if 'notify_' in section and key == 'type' and section.replace('notify_', '') in trigger:
                    m = getattr(self, value)
                    m(section)

    def fmsnntp(self, config_section):
        fms_host = self.ui.config(config_section, 'fmshost')
        fms_port = self.ui.config(config_section, 'fmsport')
        fms_user = self.ui.config(config_section, 'fmsuser')
        fms_groups = self.ui.config(config_section, 'fmsgroups')
        updatestatic_template_path = self.ui.config(config_section, 'updatestatic_message_template')
        bundle_template_path = self.ui.config(config_section, 'bundle_message_template')

        if fms_host and fms_port and fms_user and fms_groups:
            print "Sending notification..."
            server = FMS_NNTP(fms_host, fms_user, fms_groups, int(fms_port))

            if self.notify_data['type'] == 'updatestatic':
                result = server.post_updatestatic(self.notify_data, template_path=updatestatic_template_path)
            elif self.notify_data['type'] == 'bundle':
                result = server.post_bundle(self.notify_data, template_path=bundle_template_path)

            server.quit()

            print "NNTP result: %s" % str(result)

class _static_composer(object):
    """
    a helper class to compose the ClientPutComplexDir
    """
    #@    @+others
    #@+node:__init__
    def __init__(self, repo, cmd):
        """ """

        self._rootdir = repo.url()[5:] + '/.hg/'
        self._index = 0
        self._fileitemlist = {}
        self._databuff = ''
        self._cmd = cmd
        self._indexname = None

        a = dircache.listdir(self._rootdir)

        for s in a:
            if s == 'hgrc':
                pass # it may contains private/local config!! -> forbitten
            elif s == 'store':
                pass # store parsed later explizit
            elif s == 'wlock':
                pass # called from hook, ignore
            elif os.path.isdir(self._rootdir +'/'+s):
                pass # unexpected dir, ignore
            else:
                self._addItem('', s)

        self._parseDir('store')

    def _parseDir(self, dir):
        a = dircache.listdir(self._rootdir + dir)
        dircache.annotate(self._rootdir + dir, a)
        for s in a:
            if s[ - 1:] == '/':
                self._parseDir(dir + '/' + s[: - 1])
            elif s[ - 4:] == 'lock':
                pass # called from hook, ignore
            else:
                self._addItem(dir, s)

    def _addItem(self, dir, filename):
        """ """
        if dir != "":
            dir = dir + '/'

        virtname = dir + filename
        realname = self._rootdir + virtname

        f = open(realname)
        content = f.read()
        f.close()

        self._databuff = self._databuff + content
        idx = str(self._index)

        self._cmd.setItem("Files." + idx + ".Name", ".hg/" + virtname)
        self._cmd.setItem("Files." + idx + ".UploadFrom", "direct")
        self._cmd.setItem("Files." + idx + ".Metadata.ContentType", "text/plain")
        self._cmd.setItem("Files." + idx + ".DataLength", str(len(content)))

        self._index = self._index + 1
        
    def addIndex(self, indexpage):
        idx = str(self._index)
        self._cmd.setItem("Files." + idx + ".Name", "index.html")
        self._cmd.setItem("Files." + idx + ".UploadFrom", "direct")
        self._cmd.setItem("Files." + idx + ".Metadata.ContentType", "text/html")
        self._cmd.setItem("Files." + idx + ".DataLength", str(len(indexpage)))
        self._index = self._index + 1
        self._cmd.setItem("DefaultName", "index.html")
        
        self._databuff = self._databuff + indexpage

    def getCmd(self):
        return self._cmdbuff + "Data\n"

    def getRawCmd(self):
        return self._cmdbuff + "Data\n" + self._databuff
    
    def getData(self):
        return self._databuff

# every command must take a ui and and repo as arguments.
# opts is a dict where you can find other command line flags
#
# Other parameters are taken in order from items on the command line that
# don't start with a dash.  If no default value is given in the parameter list,
# they are required
def fcp_bundle(ui, repo, **opts):
    """write bundle to CHK/USK
    the bundel will be inserted as CHK@ if no uri is given
    see hg help bundle for bundle options
    """

    # make tempfile
    tmpfd, tmpfname = tempfile.mkstemp('fcpbundle')

    #delete it, the bundle function don't like prexisting files
    os.remove(tmpfname)

    #create bundle file, call the origin bundle funcrion
    commands.bundle(ui, repo, tmpfname, **opts)

    #read the bundle
    f = open(tmpfname, 'r+')
    bundledata = f.read()

    #delete the tempfile again
    os.remove(tmpfname)

    print "insert now. this may take a while..."
  
    try:
        conn = HgFCPConnection(ui, **opts)
        resulturi = hgBundlePut(conn, bundledata)
    except Exception, e:
        print e
        return

    notify_data = {'uri' : resulturi,
                       'type' : 'bundle',
                       'repository' : '%s' % repo.root.split('/')[ - 1],
                       'base' : opts.get('base'),
                       'rev' : opts.get('rev')}

    notifier = Notifier(ui, notify_data, autorun=True)

def fcp_unbundle(ui, repo, uri , **opts):
    """unbundle from CHK/USK"""

    # make tempfile
    tmpfd, tmpfname = tempfile.mkstemp('fcpbundle')

    unbundle = None

    try:
        conn = HgFCPConnection(ui, **opts)
        unbundle = hgBundleGet(conn, uri)
    except Exception, e:
        print e

    if unbundle:
        changes = unbundle
        f = open(tmpfname, 'wb')
        f.write(changes)
        f.close()
        commands.unbundle(ui, repo, tmpfname, **opts)

    os.remove(tmpfname)

def fcp_createstatic(ui, repo, uri=None, **opts):
    """put the repo into freenet for access via static-http, updatedable (not implemented jet)
    """

    pass

def fcp_updatestatic(ui, repo, **opts):
    """update the repo in freenet for access via static-http"""

    updatestatic_hook(ui, repo, None, **opts)

def updatestatic_hook(ui, repo, hooktype, node=None, source=None, **kwargs):
    """update static """

    id = "freenethgid" + str(int(time.time() * 1000000))
    
    if not kwargs.get('fcphost'):
        host = ui.config('freenethg', 'fcphost')
    else:
        host = kwargs.get('fcphost')
        
    if not kwargs.get('fcpport'):
        port = ui.config('freenethg', 'fcpport')
    else:
        port = kwargs.get('fcpport')

    if not kwargs.get('uri'):
        uri = ui.config('freenethg', 'inserturi')
    else:
        uri = kwargs.get('uri')

    cmd = FCPCommand("ClientPutComplexDir")
    cmd.setItem('Verbosity', -1)
    cmd.setItem('URI', uri)
    cmd.setItem('MaxRetries', -1)
    cmd.setItem('DontCompress', 'false')
    cmd.setItem('PriorityClass', '1')
    
    composer = _static_composer(repo, cmd)
    page_maker = IndexPageMaker()
    indexpage = page_maker.get_index_page(ui)
    composer.addIndex(indexpage)

    print "site composer done."
    print "insert now. this may take a while..."

    result = None

    try:
        conn = HgFCPConnection(ui, **kwargs)
        conn.sendCommand(cmd, composer.getData())
    
        while True:
            msg = conn.readEndMessage()
        
            if msg.isMessageName('PutSuccessful'):
                result = msg.getValue('URI')
                if None == hooktype:
                    print "Insert Succeeded at: " + result
                break
        
            if msg.isMessageName('ProtocolError'):
                raise Exception("ProtocolError(%d) - %s: %s" % (msg.getIntValue('Code'), msg.getValue('CodeDescription'), msg.getValue('ExtraDescription')))
            
            if msg.isMessageName('PutFailed'):
                raise Exception("This should really not happen!")
        
            if msg.isMessageName('SimpleProgress'):
                print "Succeeded: %d  -  Required: %d  -  Total: %d  -  Failed: %d  -  Final: %s" % (msg.getIntValue('Succeeded'), msg.getIntValue('Required'), msg.getIntValue('Total'), msg.getIntValue('FatallyFailed'), msg.getValue('FinalizedTotal'))
                continue

            #print msg.getMessageName()

    except Exception, e:
        print e
        return
    
    if result:
        notify_data = {'uri' : result,
                       'type' : 'updatestatic'}
        notifier = Notifier(ui, notify_data, autorun=True)


def updatestatic_hook2(ui, repo, hooktype, node=None, source=None, **kwargs):
    """update static """

    # if ukw not set or empty throw an error

    ukw = ui.config('freenethg', 'uploadkeyword')

    if not ukw:
        raise Exception, 'required config option »uploadkeyword« not set'

    if len(ukw.strip()) == 0:
        raise Exception, 'the keyword must contains at least one printable non-whitespace char'

    comment = repo.changelog.read(bin(node))[4]

    if ukw in comment:
        updatestatic_hook(ui, repo, hooktype, node, kwargs)

def updatestatic_hook3(ui, repo, hooktype, node=None, source=None, **kwargs):
    """update static """

    # if ukw not set or empty throw an error

    ukw = ui.config('freenethg', 'uploadkeyword')

    if not ukw:
        raise ConfigError, 'required config option »uploadkeyword« not set'

    # the keyword must contains at least one printable non-whitespace char
    # test_ukw = ukw.trim()

    #if test_ukw == "":
    #    raise ConfigError, 'required config option »uploadkeyword« cant be empty'

    # if kw not set or empty throw an error

    # message = getCommitMessage
    # if messege not contains kw
    #     nothing to do, return

    # stp = checkNodeForSiteToolPlugin
    # if !stp     
    #    # SiteToolPlugin not installed, print a warning and fallback to simple plain isert
    #    updatestatic_hook(ui, repo, hooktype, node, source, kwargs)

    # We expect lost inserts, so don't belive in the parent passed into hook
    # oldTip = getLatestFromFreenet

    # filelist = getChangedFiles(oldTip, newTip)

    # openSession (SiteToolPlugin)

    # applyFiles(fileList)

    # commitSession

    # ende




remoteopts = [
    ('e', 'ssh', '', 'specify ssh command to use'),
    ('', 'remotecmd', '', 'specify hg command to run on the remote side'),
]

fcpopts = [
    ('', 'fcphost', '', 'specify fcphost if not 127.0.0.1'),
    ('', 'fcpport', 0, 'specify fcpport if not 9481'),
    ('', 'fcptimeout', 0, 'specify fcp timeout if not 5 minutes'),
]

cmdtable = {
       # cmd name        function call
       "fcp-bundle": (fcp_bundle,
                      [('f', 'force', None, 'run even when remote repository is unrelated'),
                       ('r', 'rev', [], 'a changeset you would like to bundle'),
                       ('', 'base', [], 'a base changeset to specify instead of a destination'),
                       ('a', 'all', None, 'bundle all changesets in the repository'),
                       ('', 'uri', None, 'use insert uri generate chk'),
                      ] + remoteopts + fcpopts,
                      'hg fcp-bundle [--uri INSERTURI] [-f] [-r REV]... [--base REV]... [DEST]'),
       "fcp-unbundle": (fcp_unbundle,
                        [('u', 'update', None, 'update to new tip if changesets were unbundled'),
                         ] + fcpopts,
                        'hg fcp-unbundle [-u] FREENETKEY'),
       "fcp-updatestatic": (fcp_updatestatic,
                        [('', 'uri', '', 'use insert uri instead from hgrc')
                         ] + fcpopts,
                        'hg fcp-updatestatic [--uri INSERTURI]')
}
