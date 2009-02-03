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
import shelve
import urlparse
import ConfigParser
import errno

from nntplib import NNTP
from StringIO import StringIO
from string import Template

from mercurial import hg
from mercurial.i18n import _
from mercurial import changelog
from mercurial import commands
from mercurial import localrepo
from mercurial import manifest
from mercurial import repo, util
from mercurial.node import bin

#
# fcp rape begin
#

# the stuff below is treated as lib, so it should not refer to hg or other non-python-builtin stuff

REQUIRED_NODE_VERSION = 1183
REQUIRED_NODE_BUILD = -1
REQUIRED_EXT_VERSION = 26

DEFAULT_FCP_HOST = "127.0.0.1"
DEFAULT_FCP_PORT = 9481
DEFAULT_FCP_TIMEOUT = 300

# utils
def _getUniqueId():
    """Allocate a unique ID for a request"""
    timenum = int( time.time() * 1000000 )
    randnum = random.randint( 0, timenum )
    return "id" + str( timenum + randnum )

class FCPLogger(object):
    """log fcp traffic"""

    def __init__(self, filename=None):
        self.logfile = sys.stdout
    
    def write(self, line):
        self.logfile.write(line + '\n')

# asynchronous stuff (single thread)
class FCPIOConnection(object):
    """class for real i/o and format helpers"""

    def __init__(self, host, port, timeout, logger=None):
        self._logger = logger
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.settimeout(timeout)
        try:
            self.socket.connect((host, port))
        except Exception, e:
            raise Exception("Failed to connect to %s:%s - %s" % (host, port, e))
        if (None != self._logger):
            self._logger.write("init: connected to %s:%s (timeout %ds)" % (host, port, timeout))

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
        if (None != self._logger):
            self._logger.write("in: <"+str(len(buf))+" Bytes of data read>")
        return buf

    def skip(self, n):
        remaining = n
        while remaining > 0:
            chunk = self.socket.recv(remaining)
            chunklen = len(chunk)
            if not chunk:
                raise Exception("FCP socket closed by node")
            remaining -= chunklen
        if (None != self._logger):
            self._logger.write("in: <"+str(n)+" Bytes of data skipped>")

    def readEndMessage(self):
        #the first line is the message name
        messagename = self._readline()

        if (None != self._logger):
            self._logger.write("in: "+messagename)

        items = {}
        while True:
            line = self._readline()

            if (None != self._logger):
                self._logger.write("in: "+line)

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
        if (None != self._logger):
            self._logger.write("out: "+line)
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
        if (None != self._logger):
            self._logger.write("out: <"+str(len(data))+" Bytes of data>")
        self.socket.sendall(data)

class FCPConnection(FCPIOConnection):
    """class for low level fcp protocol i/o"""

    def __init__(self, host, port, timeout, logger=None, noversion=None):
        """c'tor leaves a ready to use connection (hello done)"""
        FCPIOConnection.__init__(self, host, port, timeout, logger)
        self._helo(noversion)

    def _helo(self, noversion):
        """perform the initial FCP protocol handshake"""
        name = _getUniqueId()
        self._sendMessage("ClientHello", Name=name, ExpectedVersion="2.0")
        msg = self.readEndMessage()
        if not msg.isMessageName("NodeHello"):
            raise Exception("Node helo failed: %s" % (msg.getMessageName()))

        # check versions
        if not noversion:
            version = msg.getIntValue("Build")
            if version < REQUIRED_NODE_VERSION:
                if version == (REQUIRED_NODE_VERSION-1):
                    revision = msg.getValue("Revision")
                    if not revision == '@custom@':
                        revision = int(revision)
                        if revision < REQUIRED_NODE_BUILD:
                            raise Exception("Node to old. Found build %d, but need minimum build %d" % (revision, REQUIRED_NODE_BUILD))
                else:
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

    def __init__(self, name, identifier=None):
        self._name = name
        self._items = {}
        if None == identifier:
            self._items['Identifier'] = _getUniqueId()
        else:
            self._items['Identifier'] = identifier

    def getCommandName(self):
        return self._name

    def getItems(self):
        return self._items

    def setItem(self, name, value):
        self._items[name] = value

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
    """class for managing/running FCPJobs on a single connection"""
    
# the stuff above is treated as lib, so it should not refer to hg or other non-python-builtin stuff

# protocol handler for "fcp://... urls
# this makes "hg clone fcp://127.0.0.1:9481/USK@blah/" work

hg.schemes['fcp'] = sys.modules[__name__]

def parseurl(fcp_url):
    """ parse an url fcp://<user>:<password>@<host>:<port>/<freenetkey>;<connectionparams>?<commandparams>
        and return freeneturi, nodeconf, commandparams, auth
    """
    tupleli = urlparse.urlparse("http"+fcp_url[3:])
    nodeconf = {}
    if tupleli.path == '':
        nodeconf['fcphost'] = None
        nodeconf['fcpport'] = None
        freeneturi = tupleli.netloc
    else:
        if tupleli.hostname:
            nodeconf['fcphost'] = tupleli.hostname
        else:
            nodeconf['fcphost'] = None
        if tupleli.port:
            nodeconf['fcpport'] = tupleli.port
        else:
            nodeconf['fcpport'] = None
        freeneturi = tupleli.path

    if freeneturi[:1] == '/':
        freeneturi = freeneturi[1:]

    if tupleli.params :
        for p in tupleli.params.split('&'):
            i, v = p.split('=')
            if i == 'TimeOut':
                nodeconf['fcptimeout'] = int(v)
            if i == 'FCPLog':
                nodeconf['fcplog'] = bool(v)
            if i == 'NoVersion':
                nodeconf['fcpnoversion'] = bool(v)

    commandparams = {}
    if tupleli.query :
        for p in tupleli.query.split('&'):
            i, v = p.split('=')
            commandparams[i] = v

    auth = {}           
    if (tupleli.username) and (tupleli.password):
        auth['fcpuser'] = tupleli.username
        auth['fcppass'] = tupleli.password
    else:
        auth = None         

    return freeneturi, nodeconf, commandparams, auth

class fcprangereader(object):

    def __init__(self, ui, fcpcache, uri, fcpconnection, commandparams, auth):
        self._ui = ui
        self._fcpcache = fcpcache
        self._uri = uri
        self._fcpconnection = fcpconnection
        if not commandparams:
            self._commandparams = {}
        else:
            self._commandparams = commandparams
        self._auth = auth
        self._pos = 0
        self._data = None
        self._datasize = -1
        #debug stuff
        self._testid = _getUniqueId()

    def seek(self, pos):
        self._pos = pos
        #print "frr set pos ", self._testid, "  -> ", pos

    def read(self, bytes=None):
        self._getData();
        #print "frr read bytes", self._testid, "  -> ", self._pos, "  -> ", bytes
        if bytes == None and self._pos == 0:
            return self._data
        retdata = self._data[self._pos:(self._pos +bytes)]
        self._pos += bytes
        return retdata
        
    def _getData(self):
        if self._data:
            return

        try:
            self._data = self._fcpcache[self._uri]
            return
        except KeyError:
            #not in cache, ignore
            pass

        getcmd = FCPCommand('ClientGet', self._testid)
        getcmd.setItem('Verbosity', -1)
        getcmd.setItem('URI', self._uri)
        if self._commandparams.get('MaxRetries'):
            getcmd.setItem('MaxRetries', self._commandparams['MaxRetries'])
        else:
            getcmd.setItem('MaxRetries', 5)
        if self._commandparams.get('PriorityClass'):
            getcmd.setItem('PriorityClass', self._commandparams['Priority'])
        else:
            getcmd.setItem('PriorityClass', 1)
        getcmd.setItem('ReturnType', 'direct')
        self._fcpconnection.sendCommand(getcmd)

        while True:
            msg = self._fcpconnection.readEndMessage()
        
            if msg.isMessageName('AllData'):
                self._datasize = msg.getIntValue('DataLength')
                self._data = self._fcpconnection.read(self._datasize)
                self._fcpcache[self._uri] = self._data
                return

            if msg.isMessageName('ProtocolError'):
                raise Exception("ProtocolError(%d) - %s: %s" % (msg.getIntValue('Code'), msg.getValue('CodeDescription'), msg.getValue('ExtraDescription')))
            
            if msg.isMessageName('GetFailed'):
                if (msg.getIntValue('Code')==27):
                    self._uri = msg.getValue('RedirectURI')
                    return self.read(bytes)
                raise Exception("GetFailed(%d) - %s: %s" % (msg.getIntValue('Code'), msg.getValue('ShortCodeDescription'), msg.getValue('CodeDescription')))
        
            if msg.isMessageName('SimpleProgress'):
                self._ui.status("Succeeded: %d  -  Required: %d  -  Total: %d  -  Failed: %d  -  Final: %s\n" % (msg.getIntValue('Succeeded'), msg.getIntValue('Required'), msg.getIntValue('Total'), msg.getIntValue('FatallyFailed'), msg.getValue('FinalizedTotal')))
                continue


def build_opener(ui, fcpcache, fcpconnection, commandparams, auth):

    def opener(base):
        """return a function that opens files over fcp"""
        p = base 
        def o(path, mode="r"):
            uri = p+'/'+ path 
            return fcprangereader(ui, fcpcache, uri, fcpconnection, commandparams, auth)
        return o

    return opener

class fcprepository(localrepo.localrepository):
    def __init__(self, ui, freeneturi, fcpconnecton, commandparams, auth):
        if freeneturi[len(freeneturi)-1] == '/':
            self.path = freeneturi[:len(freeneturi)-1]
        else:
            self.path = freeneturi
        self.path = self.path+'/.hg'
        self.ui = ui
        self._fcpcache = {}

        opener = build_opener(ui, self._fcpcache, fcpconnecton, commandparams, auth)
        self.opener = opener(self.path)

        # find requirements
        try:
            requirements = self.opener("requires").read().splitlines()
        except IOError, inst:
            if inst.errno != errno.ENOENT:
                raise
            # check if it is a non-empty old-style repository
            try:
                self.opener("00changelog.i").read(1)
            except IOError, inst:
                if inst.errno != errno.ENOENT:
                    raise
                # we do not care about empty old-style repositories here
                msg = _("'%s' does not appear to be an hg repository") % freeneturi
                raise repo.RepoError(msg)
            requirements = []

        # check them
        for r in requirements:
            if r not in self.supported:
                raise repo.RepoError(_("requirement '%s' not supported") % r)

        # setup store
        if "store" in requirements:
            self.encodefn = util.encodefilename
            self.decodefn = util.decodefilename
            self.spath = self.path + "/store"
        else:
            self.encodefn = lambda x: x
            self.decodefn = lambda x: x
            self.spath = self.path
        self.sopener = util.encodedopener(opener(self.spath), self.encodefn)

        self.manifest = manifest.manifest(self.sopener)
        self.changelog = changelog.changelog(self.sopener)
        self.tagscache = None
        self.nodetagscache = None
        self.encodepats = None
        self.decodepats = None

    def url(self):
        return self.path

    def local(self):
        return False

    def lock(self, wait=True):
        raise util.Abort(_('cannot lock fcp repository'))


def instance(ui, fcp_url, create):
    if create:
        raise util.Abort(_('creating repository via fcp not supported jet.'))
    freeneturi, nodeconf, commandparams, auth = parseurl(fcp_url)

    logger = None
    if nodeconf.get('fcplog'):
        logger = HgFCPLogger(ui)
    conn = HgFCPConnection(logger, ui, **nodeconf) 
    return fcprepository(ui, freeneturi, conn, commandparams, auth)

# protokol handler end

class HgFCPConnection(FCPConnection):
    
    def __init__(self, logger, ui, **opts):

        host = ui.config('freenethg', 'fcphost')
        port = ui.config('freenethg', 'fcpport')
        timeout = ui.config('freenethg', 'fcptimeout')
        noversion = ui.config('freenethg', 'fcpnoversion')

        if host == None:
            host = os.environ.get("FCP_HOST", DEFAULT_FCP_HOST)
        if port == None:
            port = os.environ.get("FCP_PORT", DEFAULT_FCP_PORT)
        if timeout == None:
            timeout = os.environ.get("FCP_TIMEOUT", DEFAULT_FCP_TIMEOUT)
        if noversion == None:
            noversion = os.environ.get("FCP_NOVERSION", None)
               
        # command line overwrites
        if opts.get('fcphost'):
            host = opts['fcphost']
        if opts.get('fcpport'):
            port = opts['fcpport']
        if opts.get('fcptimeout'):
            timeout = opts['fcptimeout']
        if opts.get('fcpnoversion'):
            noversion = True
                
        FCPConnection.__init__(self, host, int(port), timeout, logger, noversion)
        
class HgFCPLogger(FCPLogger):

    def __init__(self, ui):
        FCPLogger.__init__(self, '-')
        self.ui = ui
    
    def write(self, line):
        self.ui.write(line + '\n')
            
        
def makeFCPLogger(ui, **opts):
    fcplogger = ui.config('freenethg', 'fcplog')
    if opts.get('fcplog'):
        fcplogger = HgFCPLogger(ui)
    return fcplogger
        
def hgBundlePut(ui, connection, uri, data, dontcompress):
    
    putcmd = FCPCommand('ClientPut')
    putcmd.setItem('Verbosity', -1)
    putcmd.setItem('URI', uri)
    putcmd.setItem('MaxRetries', -1)
    putcmd.setItem('Metadata.ContentType', 'mercurial/bundle')
    if dontcompress:
        putcmd.setItem('DontCompress', 'true')
    else:
        putcmd.setItem('DontCompress', 'false')
    putcmd.setItem('PriorityClass', '1')
    putcmd.setItem('UploadFrom', 'direct')
    putcmd.setItem('DataLength', len(data))
    
    connection.sendCommand(putcmd, data)

    while True:
        msg = connection.readEndMessage()
        
        if msg.isMessageName('PutFetchable') or msg.isMessageName('PutSuccessful'):
            return msg.getValue('URI')
        
        if msg.isMessageName('ProtocolError'):
            raise Exception("ProtocolError(%d) - %s: %s" % (msg.getIntValue('Code'), msg.getValue('CodeDescription'), msg.getValue('ExtraDescription')))
            
        if msg.isMessageName('PutFailed'):
            raise Exception("This should really not happen!")
        
        if msg.isMessageName('SimpleProgress'):
            ui.status("Succeeded: %d  -  Required: %d  -  Total: %d  -  Failed: %d  -  Final: %s\n" % (msg.getIntValue('Succeeded'), msg.getIntValue('Required'), msg.getIntValue('Total'), msg.getIntValue('FatallyFailed'), msg.getValue('FinalizedTotal')))
            continue

#        print msg.getMessageName()

def hgBundleGet(ui, connection, uri):
    
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
            size = msg.getIntValue('DataLength')
            return connection.read(size)
        
        if msg.isMessageName('ProtocolError'):
            raise Exception("ProtocolError(%d) - %s: %s" % (msg.getIntValue('Code'), msg.getValue('CodeDescription'), msg.getValue('ExtraDescription')))
            
        if msg.isMessageName('GetFailed'):
            if (msg.getIntValue('Code')==24):
                return hgBundleGet(ui, connection, msg.getValue('RedirectURI'))
            raise Exception("GetFailed(%d) - %s: %s" % (msg.getIntValue('Code'), msg.getValue('ShortCodeDescription'), msg.getValue('CodeDescription')))
        
        if msg.isMessageName('SimpleProgress'):
            ui.status("Succeeded: %d  -  Required: %d  -  Total: %d  -  Failed: %d  -  Final: %s\n" % (msg.getIntValue('Succeeded'), msg.getIntValue('Required'), msg.getIntValue('Total'), msg.getIntValue('FatallyFailed'), msg.getValue('FinalizedTotal')))
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
                ui.write_err("Error while processing template from %s:\n" % ui.config('freenethg', 'indextemplate'))
                print e
                ui.warn("Using default template\n")
                page = self.get_default_index_page(default_data)
        else:
            page = self.get_default_index_page(default_data)

        return page

class FMS_NNTP(NNTP):
    """class for posts to newsgroups on nntp servers"""

    nntp_msg_template = Template("""From: $fms_user\nNewsgroups: $fms_groups\nSubject: $subject\nContent-Type: text/plain; charset=UTF-8\n\n$body""")

    def __init__(self, ui, host, fms_user, groups, port=1119):
        self.ui = ui
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
            self.ui.write_err("Error while processing template from %s:" % template_path)
            self.ui.write_err(e)
            self.ui.write_err("Using default template")

        return (subject_addon, user_template)

    def post_updatestatic(self, notify_data, template_path=None):
        uri = notify_data['uri']
        uri = uri.endswith('/') and uri[: - 1] or uri

        #FIXME
        # this fails on CHK, KSK , SSK@../not-a-usk-format
        #repository_name = uri.split('/')[1]
        #repository_version = uri.split('/')[2]

        if template_path:
            subject_addon, user_template = self._load_template(template_path)
        else:
            subject_addon = user_template = None

        if user_template:
            body = Template(user_template)
        else:
            body = Template('This is an automated message of pyFreenetHg.\n\nMercurial repository update:\n$uri')

        body = body.substitute({'uri':uri})
        #FIXME
        # this fails on CHK, KSK , SSK@../not-a-usk-format
        #subject = 'Repository "%s" updated, Ed. %s' % (repository_name, repository_version)
        subject = 'Repository "%s" updated.' % uri

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
            self.ui.status("Sending notification...\n")
            server = FMS_NNTP(self.ui, fms_host, fms_user, fms_groups, int(fms_port))

            if self.notify_data['type'] == 'updatestatic':
                result = server.post_updatestatic(self.notify_data, template_path=updatestatic_template_path)
            elif self.notify_data['type'] == 'bundle':
                result = server.post_bundle(self.notify_data, template_path=bundle_template_path)

            server.quit()

            self.ui.status("NNTP result: %s\n" % str(result))

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

    ui.status("insert now. this may take a while...\n")

    fcplogger = makeFCPLogger(ui, **opts)
    try:
        conn = HgFCPConnection(fcplogger, ui, **opts)
        if opts.get('fcpdontcompress'):
            dontcompress = True
        else:
            dontcompress = False
        if opts.get('uri'):
            inserturi = opts.get('uri')
        else:
            inserturi = 'CHK@'
        resulturi = hgBundlePut(ui, conn, inserturi, bundledata, dontcompress)
        ui.write("Insert Succeeded at: %s\n" % (resulturi))
    except Exception, e:
        print e
        return

    bundle_history_path = ui.config('freenethg','bundlehistory')
    if bundle_history_path:
        bundle_history = shelve.open(bundle_history_path, protocol=2, writeback=True)
        now = time.mktime(time.localtime())
        bundle_history.update({resulturi:{'base' : opts.get('base'),
                                          'rev' : opts.get('rev'),
                                          'time' : now}})
        bundle_history.close()

    if not kwargs.get('nonotify'):
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

    fcplogger = makeFCPLogger(ui, **opts)

    try:
        conn = HgFCPConnection(fcplogger, ui, **opts)
        unbundle = hgBundleGet(ui, conn, uri)
    except Exception, e:
        print e

    if unbundle:
        changes = unbundle
        f = open(tmpfname, 'wb')
        f.write(changes)
        f.close()
        commands.unbundle(ui, repo, tmpfname, **opts)

    os.remove(tmpfname)

def fcp_updatestatic(ui, repo, **opts):
    """update the repo in freenet for access via static-http"""

    hookname = ui.config('hooks','commit')
    if hookname:
        if 'updatestatic_hook3' in hookname:
            opts['forcerun'] = True
            updatestatic_hook3(ui, repo, None, repo.changelog.tip(), **opts)
            return

    updatestatic_hook(ui, repo, None, **opts)

def updatestatic_hook(ui, repo, hooktype, node=None, source=None, **kwargs):
    """update static """

    username = ui.config('freenethg', 'commitusername')

    if not username:
        ui.warn("No username set in .hg/hgrc!\n")

    if not kwargs.get('uri'):
        uri = ui.config('freenethg', 'inserturi')
    else:
        uri = kwargs.get('uri')

    if not uri:
        raise util.Abort("freenethg not (properly) configured and no insert uri given. Abort.")

    if kwargs.get('globalput'):
        doglobal = True
    else:
        doglobal = False

    putid = _getUniqueId()

    cmd = FCPCommand("ClientPutComplexDir", putid)
    cmd.setItem('Verbosity', -1)
    cmd.setItem('URI', uri)
    cmd.setItem('MaxRetries', -1)
    if kwargs.get('fcpdontcompress'):
        cmd.setItem('DontCompress', 'true')
    else:
        cmd.setItem('DontCompress', 'false')
    cmd.setItem('PriorityClass', '1')
    if doglobal:
        cmd.setItem('Global', 'true')
        cmd.setItem('Persistence', 'forever')

    composer = _static_composer(repo, cmd)
    page_maker = IndexPageMaker()
    indexpage = page_maker.get_index_page(ui)
    composer.addIndex(indexpage)

    ui.status("site composer done.\n")
    ui.status("insert now. this may take a while...\n")

    result = None

    fcplogger = makeFCPLogger(ui, **kwargs)
    try:
        conn = HgFCPConnection(fcplogger, ui, **kwargs)
        if doglobal:
            wcmd = FCPCommand("WatchGlobal")
            wcmd.setItem('Global', 'true')
            wcmd.setItem('Verbosity', -1)
            wcmd.setItem('Enabled', 'true')
            conn.sendCommand(wcmd)
        conn.sendCommand(cmd, composer.getData())

        while True:
            msg = conn.readEndMessage()

            if doglobal:
                if putid != msg.getValue('Identifier'):
                    continue

            if msg.isMessageName('PutSuccessful'):
                result = msg.getValue('URI')
                if None == hooktype:
                    ui.write("Insert Succeeded at: %s\n" % (result))
                break

            if msg.isMessageName('ProtocolError'):
                raise Exception("ProtocolError(%d) - %s: %s" % (msg.getIntValue('Code'), msg.getValue('CodeDescription'), msg.getValue('ExtraDescription')))

            if msg.isMessageName('PutFailed'):
                raise Exception("This should really not happen!")

            if msg.isMessageName('SimpleProgress'):
                ui.status("Succeeded: %d  -  Required: %d  -  Total: %d  -  Failed: %d  -  Final: %s\n" % (msg.getIntValue('Succeeded'), msg.getIntValue('Required'), msg.getIntValue('Total'), msg.getIntValue('FatallyFailed'), msg.getValue('FinalizedTotal')))
                continue

            if msg.isMessageName('PersistentPutDir'):
                if msg.getValue('Started') == 'true':
                    ui.status("Put queued\n")
                continue

            if msg.isMessageName('StartedCompression'):
                continue

            if msg.isMessageName('FinishedCompression'):
                continue
            
            if msg.isMessageName('PutFetchable'):
                continue

            print "unhandled: ", msg.getMessageName()

    except Exception, e:
        print e
        return

    if result and not kwargs.get('nonotify'):
        notify_data = {'uri' : result,
                       'type' : 'updatestatic'}
        notifier = Notifier(ui, notify_data, autorun=True)


def updatestatic_hook2(ui, repo, hooktype, node=None, source=None, **kwargs):
    """update static """

    # if ukw not set or empty throw an error

    ukw = ui.config('freenethg', 'uploadkeyword')

    if not ukw:
        raise util.Abort('required config option »uploadkeyword« not set')

    if len(ukw.strip()) == 0:
        raise util.Abort('the keyword must contains at least one printable non-whitespace char')

    comment = repo.changelog.read(bin(node))[4]

    if ukw in comment:
        updatestatic_hook(ui, repo, hooktype, node, kwargs)

def updatestatic_hook3(ui, repo, hooktype, node=None, source=None, **kwargs):
    """update static """

    # if ukw not set or empty throw an error
    ukw = ui.config('freenethg', 'uploadkeyword')

    if not ukw:
        raise util.Abort('required config option »uploadkeyword« not set')

    if len(ukw.strip()) == 0:
        raise util.Abort('the keyword must contains at least one printable non-whitespace char')

    if hooktype:
        tmpdat = repo.changelog.read(bin(node))
    else:
        tmpdat = repo.changelog.read(node)
    comment = tmpdat[4]
    files = tmpdat[3]

    if not ukw in comment and not kwargs.get('forcerun'): # forcerun triggered by fcp-updatestatic if hook=3
        print "not doing hook3"
        return

    print "doing hook3"

    cmd = FCPCommand("GetPluginInfo")
    cmd.setItem('PluginName', 'plugins.SiteToolPlugin.SiteToolPlugin')

    fcplogger = makeFCPLogger(ui, **kwargs)
    conn = HgFCPConnection(fcplogger, ui, **kwargs)
    conn.sendCommand(cmd)

    # no protocol error means plugin found.
    msg = conn.readEndMessage()
    if msg.isMessageName('ProtocolError'):
        raise util.Abort("ProtocolError(%d) - %s: %s" % (msg.getIntValue('Code'), msg.getValue('CodeDescription'), msg.getValue('ExtraDescription')))

    # We expect lost inserts, so don't belive in the parent passed into hook
    fnrepo = fcprepository(ui, freeneturi, conn, None, None)
    oldTip = fnrepo.changelog.tip()

    # filelist = getChangedFiles(oldTip, newTip)


#        while True:
#            msg = conn.readEndMessage()
#        
#            if msg.isMessageName('PutSuccessful'):
#                result = msg.getValue('URI')
#                if None == hooktype:
#                    ui.write("Insert Succeeded at: %s\n" % (result))
#                break
#        
#            if msg.isMessageName('ProtocolError'):
#                raise Exception("ProtocolError(%d) - %s: %s" % (msg.getIntValue('Code'), msg.getValue('CodeDescription'), msg.getValue('ExtraDescription')))
#            
#            if msg.isMessageName('PutFailed'):
#                raise Exception("This should really not happen!")
#        
#            if msg.isMessageName('SimpleProgress'):
#                ui.status("Succeeded: %d  -  Required: %d  -  Total: %d  -  Failed: %d  -  Final: %s\n" % (msg.getIntValue('Succeeded'), msg.getIntValue('Required'), msg.getIntValue('Total'), msg.getIntValue('FatallyFailed'), msg.getValue('FinalizedTotal')))
#                continue
#
#            print msg.getMessageName()
#
#    except Exception, e:
#        print e
#        return

    # openSession (SiteToolPlugin)

    # applyFiles(fileList)

    # commitSession

    # ende

def username_checker(ui, repo, hooktype, node=None, source=None, **kwargs):
    """
    pretxncommithook to prevent identity leaks, username defaults to <loginuser@host>,
    this might be a bad idea for freenet.
    """

    expectedusername = ui.config('freenethg', 'commitusername')
    usedusername = repo.changelog.read(bin(node))[1]
    if expectedusername != usedusername:
        ui.warn("Invalid username. Commit rejected to prevent identity leaks.\n")
        ui.status("Expected username '%s', but found '%s'.\n" % (expectedusername, usedusername))
        return True
    return False

def fcp_setupwizz(ui, repo, **opts):
    """a setup wizzard for hgrc"""

    def wizzpromt(msg, pat, default):
        n = ui.prompt("%s: [%s] " % (msg, default), pat, default)
        if n == default:
            return None
        return n

    def cfgget(cfg, section, item):
        try:
            return cfg.get(section, item)
        except ConfigParser.NoOptionError:
            return None

    if not ui.interactive:
        raise util.Abort("NonInteractive mode not supported.")

    lock = repo.lock()
    ismodified = False
    try:
        tmpcfg = ConfigParser.ConfigParser()
        hgrcpath = repo.join("hgrc")
        if os.path.exists(hgrcpath):
            ui.warn('Config file already exist, comments will be lost on save! (^C to abort)\n')
            tmpcfg.read(hgrcpath) 

        # username
        username = cfgget(tmpcfg, 'freenethg', 'commitusername')
        if not username:
            username = 'anonymuse'
            ismodified = True
            tmpcfg.set('freenethg', 'commitusername', username)
            tmpcfg.set('ui', 'username', username)

        newusername = wizzpromt("Enter username for freenet, used for commits", None, username)
        if newusername:
            tmpcfg.set('freenethg', 'commitusername', newusername)
            tmpcfg.set('ui', 'username', username)
            ismodified = True

        #username check hook
        ui.write("Configure precommit hook to force the just configured username is used. (recommended to prevent identity leaks)\n")
        hookname = 'python:freenethg.username_checker'
        oldhook = cfgget(tmpcfg, 'hooks', 'pretxncommit')
        ishookset = False
        hookdefault = '+'
        if oldhook:
            ui.write("hook is set to '%s'\n" % (oldhook))
            if hookname in oldhook:
                hookdefault = '.'
                ishookset = True
        else:
            ui.write("hook is not set\n")
        ui.write("\t+ set hook to '%s'\n" % (hookname))
        ui.write("\t- unset hook\n")
        ui.write("\t. leave unchanged\n")
        hookcmd = ui.prompt("Choose [+-.]: [%s] " % (hookdefault), '[+-\.]', hookdefault)

        if hookcmd == '.':
            ui.write("leaving hook unchanged.\n")
        else:
            if hookcmd == '+':
                tmpcfg.set('hooks', 'pretxncommit', hookname)
                ismodified = True
            elif hookcmd == '-':
                tmpcfg.remove_option('hooks', 'pretxncommit')
                ismodified = True
            else:
                raise util.Abort("This should not happen")

        # first try default or env settings
        testhost = os.environ.get("FCP_HOST", DEFAULT_FCP_HOST)
        testport = os.environ.get("FCP_PORT", DEFAULT_FCP_PORT)
        testtimeout = os.environ.get("FCP_TIMEOUT", DEFAULT_TIMEOUT)
        conn = None
        try:
            conn = FCPConnection(testhost, int(testport), testtimeout, None)
            ui.write("Node found from default/environment settings at '%s:%s' (timeout=%ss)\n" % (testhost, testport, testtimeout))
            ui.write("Use '-' for host, port and timeout to use it\n")
            del conn
        except:
            # force manual config
            pass

        #host
        host = cfgget(tmpcfg, 'freenethg', 'fcphost')
        if not host:
            host = '-'
        newhost = wizzpromt("Enter fcp host ('-' for default/environment settings)", None, host)
        if newhost:
            if newhost == '-':
                tmpcfg.remove_option('freenethg', 'fcphost')
            else:
                tmpcfg.set('freenethg', 'fcphost', newhost)
            ismodified = True

        #port
        port = cfgget(tmpcfg, 'freenethg', 'fcpport')
        if not port:
            port = '-'
        newport = wizzpromt("Enter fcp port ('-' for default/environment settings)", None, port)
        if newport:
            if newport == '-':
                tmpcfg.remove_option('freenethg', 'fcpport')
            else:
                tmpcfg.set('freenethg', 'fcpport', newport)
            ismodified = True

        #timeout
        timeout = cfgget(tmpcfg, 'freenethg', 'fcptimeout')
        if not timeout:
            timeout = '-'
        newtimeout = wizzpromt("Enter fcp timeout ('-' for default/environment settings)", None, timeout)
        if newtimeout:
            if newtimeout == '-':
                tmpcfg.remove_option('freenethg', 'fcptimeout')
            else:
                tmpcfg.set('freenethg', 'fcptimeout', newtimeout)
            ismodified = True

        #upload keyword
        ukw = cfgget(tmpcfg, 'freenethg', 'uploadkeyword')
        if not ukw:
            ukw = '-'
        newukw = wizzpromt("Enter upload keyword ('-' to disable/remove)", None, ukw)
        if newukw:
            if newukw == '-':
                tmpcfg.remove_option('freenethg', 'uploadkeyword')
                newukw = None
            else:
                tmpcfg.set('freenethg', 'uploadkeyword', newukw)
            ismodified = True

        #now try to connect an guess around fitting settings
        if not newhost == '-':
            newhost = testhost
        if not newport == '-':
            newport = testport
        if not newtimeout == '-':
            newtimeout = testtimeout

        #SiteToolPlugin
        stpFound = False
        #new keypair message
        kpm = None    
        try:
            conn = FCPConnection(newhost, int(newport), newtimeout, None)
            ui.write("Connected to configured Node at '%s:%s' (timeout=%ss)\n" % (newhost, newport, newtimeout))
            #make a keypair
            cmd = FCPCommand("GenerateSSK")
            conn.sendCommand(cmd)
            msg = conn.readEndMessage()
            if msg.isMessageName('SSKKeypair'):
                kpm = msg                     
            #goggle for SiteTollPlugin
            cmd = FCPCommand("GetPluginInfo")
            cmd.setItem('PluginName', 'plugins.SiteToolPlugin.SiteToolPlugin')
            conn.sendCommand(cmd)
            msg = conn.readEndMessage()
            if msg.isMessageName('PluginInfo'):
                stpFound = True
            del conn
        except:
            # force manual config
            pass

        #insert uri
        iuri = cfgget(tmpcfg, 'freenethg', 'inserturi')
        if not iuri:
            iuri = '.'
        newiuri = wizzpromt("Enter insert uri ('.' for a generated one, '-' to remove)", None, iuri)
        if not newiuri and iuri == '.':
            newiuri = '.'
        if newiuri:
            if newiuri == '.':
                if kpm:
                    sitename = ui.prompt("Enter sitename for new uri:", None, "project-hg")
                    u = 'U' + kpm.getValue('InsertURI')[1:] + sitename + '/1/'
                    ui.write("New insert uri: %s\n" % (u))
                    tmpcfg.set('freenethg', 'inserturi', u)
                    ui.write("Request uri is: %s\n" % ('U' + kpm.getValue('RequestURI')[1:] + sitename + '/1/'))
                else:
                    u = ui.prompt("Could not generate new keypair. Enter full uri:", None, "CHK@")
                    tmpcfg.set('freenethg', 'inserturi', u)
            elif newiuri == '-':
                tmpcfg.remove_option('freenethg', 'inserturi')
            else:
                tmpcfg.set('freenethg', 'inserturi', newiuri)
            ismodified = True

        #commit hook
        ui.write("Configure postcommit hook to sync to freenet. (recommended)\n")
        hookname = 'python:freenethg.updatestatic_hook'
        oldhook = cfgget(tmpcfg, 'hooks', 'commit')
        ishookset = False
        hookdefault = '1'
        ukw = cfgget(tmpcfg, 'freenethg', 'uploadkeyword')
        if ukw:
            hookdefault = '2'
            if stpFound:
                hookdefault = '3'
        if oldhook:
            ui.write("hook is set to '%s'\n" % (oldhook))
            if hookname in oldhook:
                hookdefault = '.'
                ishookset = True
        else:
            ui.write("hook is not set\n")
        ui.write("\t1 simple hook, does a full upload on each commit\n")
        ui.write("\t2 simple hook, does a full upload only if commit message contains upload keyword\n")
        ui.write("\t3 simple hook, does a incremental upload only if commit message contains upload keyword\n")
        ui.write("\t- unset hook\n")
        ui.write("\t. leave unchanged\n")
        hookcmd = ui.prompt("Choose [123-.]: [%s] " % (hookdefault), '[123\-\.]', hookdefault)

        if hookcmd == '.':
            ui.write("leaving hook unchanged.\n")
        else:
            if hookcmd == '1':
                tmpcfg.set('hooks', 'commit', hookname)
                ismodified = True
            elif hookcmd in '23':
                tmpcfg.set('hooks', 'commit', hookname+hookcmd)
                ismodified = True
            elif hookcmd == '-':
                tmpcfg.remove_option('hooks', 'commit')
                ismodified = True
            else:
                raise util.Abort("This should not happen")

        if ismodified:
            fp = repo.opener("hgrc", "w", text=True)
            tmpcfg.write(fp)
            fp.close()
            ui.write("New config succesfully written.\n")
        else:
            ui.write("Nothing changed, config not written.\n")               

    finally:
        del lock

fcpopts = [
    ('', 'fcphost', '', 'specify fcphost if not 127.0.0.1'),
    ('', 'fcpport', 0, 'specify fcpport if not 9481'),
    ('', 'fcptimeout', 0, 'specify fcp timeout if not 5 minutes'),
    ('', 'fcplog', None, 'log fcp to mercurials output'),
    ('', 'fcpnoversion', None, 'omit fcp version check'),
]

fcpputopts = [
    ('', 'fcpdontcompress', None, 'if specified node compression is turned off'),
    ('', 'globalput', None, 'use gloabal queue and persistance "forever" for fcp put'),
]

notifyopts = [
    ('', 'nonotify', None, 'suppress all notifies'),
]

cmdtable = {
       # cmd name        function call
       "fcp-setupwitz": (fcp_setupwizz,
                        [],
                        'hg fcp-setupwitz'),
       "fcp-bundle": (fcp_bundle,
                      [('f', 'force', None, 'run even when remote repository is unrelated'),
                       ('r', 'rev', [], 'a changeset you would like to bundle'),
                       ('', 'base', [], 'a base changeset to specify instead of a destination'),
                       ('a', 'all', None, 'bundle all changesets in the repository'),
                       ('', 'uri', '', 'use insert uri generate chk'),
                      ] + commands.remoteopts + notifyopts + fcpopts + fcpputopts,
                      'hg fcp-bundle [--uri INSERTURI] [-f] [-r REV]... [--base REV]... [DEST]'),
       "fcp-unbundle": (fcp_unbundle,
                        [('u', 'update', None, 'update to new tip if changesets were unbundled'),
                         ] + fcpopts,
                        'hg fcp-unbundle [-u] FREENETKEY'),
       "fcp-updatestatic": (fcp_updatestatic,
                        [('', 'uri', '', 'use insert uri instead from hgrc')
                         ] + notifyopts + fcpopts + fcpputopts,
                        'hg fcp-updatestatic [--uri INSERTURI]'),
}

def test():
    pass

if __name__ == '__main__':
    test()
