#!/usr/bin/env python
# -*- coding: utf-8 -*-
# This program is free software. It comes without any warranty, to
# the extent permitted by applicable law. You can redistribute it
# and/or modify it under the terms of the Do What The Fuck You Want
# To Public License, Version 2, as published by Sam Hocevar. See
# http://sam.zoy.org/wtfpl/COPYING for more details. */


import os, time
import tempfile
import sys
import dircache
import fcp

from nntplib import NNTP
from StringIO import StringIO
from string import Template

from mercurial import hg
from mercurial import commands
from mercurial import repo,cmdutil,util,ui,revlog,node
from mercurial.node import bin

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

    def post_updatestatic(self,uri):

        repository_name = uri.split('/')[1]

        body = 'This is an automated message of pyFreenetHg.\n\nMercurial repository update:\n%s' % uri

        template_data = {'body':body,
                         'subject':'Update of repository "%s"' % repository_name,
                         'fms_user':self.fms_user,
                         'fms_groups':self.fms_groups,}

        article = StringIO(self.nntp_msg_template.substitute(template_data))
        result = self.post(article)
        article.close()

        return result

class myFCP(fcp.FCPNode):

    def putraw(self, id, rawcmd, async=False):
        """
        Inserts a raw command.
        This is intended for testing and development, not for common use

        Arguments:
            - id - job id, must be the same as identifier in raw command (if any)
            - rawcmd - data passed as is to the node
            - async - whether to do the job asynchronously, returning a job ticket
              object (default False)
        """

        opts = {}
        opts['async'] = async
        opts['rawcmd'] = rawcmd

        return self._submitCmd(id, "", **opts)

    def putraw2(self, id, rawcmd):
        """
        mine ;) verbosity hacking
        do not print the command
        """

        self.verbosity = fcp.INFO
        ticket = self.putraw(id, rawcmd, True)
        ticket.waitTillReqSent()
        self.verbosity = fcp.DETAIL
        return ticket.wait()

class _static_composer(object):
    """
    a helper class to compose the ClientPutComplexDir
    """
    #@    @+others
    #@+node:__init__
    def __init__(self, repo):
        """ """

        self._rootdir = repo.url()[5:] + '/.hg/'
        self._index = 0
        self._fileitemlist = {}
        self._databuff = ''
        self._cmdbuff = ''
        self._indexname = None

        a = dircache.listdir(self._rootdir)

        for s in a:
            if s == 'hgrc':
                pass # it may contains private/local config!! -> forbitten
            elif s == 'store':
                pass # store parsed later explizit
            elif s == 'wlock':
                pass # called from hook, ignore
            else:
                self._addItem('', s)

        self._parseDir('store')

    def _parseDir(self, dir):
        a = dircache.listdir(self._rootdir + dir)
        dircache.annotate(self._rootdir + dir, a)
        for s in a:
            if s[-1:] == '/':
                self._parseDir(dir + '/' + s[:-1])
            elif s[-4:] == 'lock':
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

        self._cmdbuff = self._cmdbuff + "Files."+idx+".Name=.hg/" + virtname + '\n'
        self._cmdbuff = self._cmdbuff + "Files."+idx+".UploadFrom=direct" + '\n'
        self._cmdbuff = self._cmdbuff + "Files."+idx+".Metadata.ContentType=text/plain" + '\n'
        self._cmdbuff = self._cmdbuff + "Files."+idx+".DataLength=" + str(len(content)) + '\n'

        self._index = self._index + 1
        
    def addIndex(self, indexpage):
        idx = str(self._index)
        self._cmdbuff = self._cmdbuff + "Files."+idx+".Name=index.html"+'\n'
        self._cmdbuff = self._cmdbuff + "Files."+idx+".UploadFrom=direct" + '\n'
        self._cmdbuff = self._cmdbuff + "Files."+idx+".Metadata.ContentType=text/html" + '\n'
        self._cmdbuff = self._cmdbuff + "Files."+idx+".DataLength=" + str(len(indexpage)) + '\n'
        self._index = self._index + 1
        self._cmdbuff = self._cmdbuff + "DefaultName=index.html\n"
        
        self._databuff = self._databuff + indexpage

    def getCmd(self):
        return self._cmdbuff + "Data\n"

    def getRawCmd(self):
        return self._cmdbuff + "Data\n" + self._databuff

# every command must take a ui and and repo as arguments.
# opts is a dict where you can find other command line flags
#
# Other parameters are taken in order from items on the command line that
# don't start with a dash.  If no default value is given in the parameter list,
# they are required
def fcp_bundle(ui, repo, **opts):
    # The doc string below will show up in hg help
    """write bundle to CHK/USK
    the bundel will be inserted as CHK@ if no uri is given
    see hg help bundle for bundle options
    """

    # setup fcp stuff
    # set server, port
    if not opts.get('fcphost'):
        opts['fcphost'] = ui.config('freenethg', 'fcphost')
    if not opts.get('fcpport'):
        opts['fcpport'] = ui.config('freenethg', 'fcpport')

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

    # now insert the data as chk (first step implementation)
    #node = fcp.FCPNode(verbosity=fcp.DETAIL)
    node = _make_node(**opts)

    insertresult = node.put(data=bundledata, priority=1, mimetype="mercurial/bundle")

    node.shutdown()

    print "bundle inserted: " + insertresult


def fcp_unbundle(ui, repo, uri , **opts):
    # The doc string below will show up in hg help
    """unbundle from CHK/USK"""

    # set server, port
    if not opts.get('fcphost'):
        opts['fcphost'] = ui.config('freenethg', 'fcphost')
    if not opts.get('fcpport'):
        opts['fcpport'] = ui.config('freenethg', 'fcpport')

    # make tempfile
    tmpfd, tmpfname = tempfile.mkstemp('fcpbundle')

    node = _make_node(**opts)

    unbundle = None

    try:
        unbundle = node.get(uri, priority=1, maxretries=5)
    except fcp.node.FCPException, e:
        print e

    node.shutdown()

    if unbundle:
        changes = unbundle[1]
        f = open(tmpfname, 'wb')
        f.write(changes)
        f.close()
        commands.unbundle(ui, repo, tmpfname, **opts)

    os.remove(tmpfname)

def _make_node(**opts):
    fcpopts = {}
    fcpopts['verbosity'] = fcp.INFO
    host = opts.get('fcphost', None)
    if host:
        fcpopts['host'] = host
    port = opts.get('fcpport', None)
    if port:
        fcpopts['port'] = port
    #return node2.FCPNode(**fcpopts)
    return myFCP(**fcpopts)

def fcp_makestatic(ui, repo, uri=None, **opts):
    """put the repo into freenet for access via static-http
    """

    id = "freenethgid" + str(int(time.time() * 1000000))

    config_uri = ui.config('freenethg', 'inserturi')

    if uri == None:
        if config_uri:
            uri = config_uri
        else:
            uri="CHK@"

    if not opts.get('fcphost'):
        opts['fcphost'] = ui.config('freenethg', 'fcphost')
    if not opts.get('fcpport'):
        opts['fcpport'] = ui.config('freenethg', 'fcpport')
    cmd = "ClientPutComplexDir\n" + "URI=" + uri + "\nIdentifier=" + id
    cmd = cmd + "\nVerbosity=-1\nPriorityClass=1\nMaxRetries=5\nDontCompress=true\n"

    composer = _static_composer(repo)

    print "Debug: " + cmd + composer.getCmd()

    print "site composer done."
    print "insert now. this may take a while..."

    node = _make_node(**opts)

    testresult = None

    try:
        #testresult = node.putraw(id, cmd + composer.getRawCmd())
        testresult = node.putraw2(id, cmd + composer.getRawCmd())
    except fcp.node.FCPException, e:
        print e

    node.shutdown()

    if testresult:
        print "success: " + testresult
        # here method for automated fms posts should be called

def fcp_createstatic(ui, repo, uri=None, **opts):
    """put the repo into freenet for access via static-http, updatedable (not implemented jet)
    """

    pass

def fcp_updatestatic(ui, repo, **opts):
    """update the repo in freenet for access via static-http
    """

    updatestatic_hook(ui, repo, None, **opts)

def updatestatic_hook(ui, repo, hooktype, node=None, source=None, **kwargs):
    """update static """


    id = "freenethgid" + str(int(time.time() * 1000000))
    host = ui.config('freenethg', 'fcphost')
    port = ui.config('freenethg', 'fcpport')

    uri = ui.config('freenethg', 'inserturi')
    #uri = "CHK@"

    fcpopts = {}
    fcpopts['verbosity'] = fcp.INFO
    fcpopts['host'] = host
    fcpopts['port'] = port

    #fcpopts['logfunc'] = ui.log
    node = myFCP(**fcpopts)

    cmd = "ClientPutComplexDir\n" + "URI=" + uri + "\nIdentifier=" + id
    cmd = cmd + "\nVerbosity=-1\nPriorityClass=1\nMaxRetries=5\nDontCompress=true\n"

    composer = _static_composer(repo)
    page_maker = IndexPageMaker()
    indexpage = page_maker.get_index_page(ui)
    composer.addIndex(indexpage)

    print "Debug: " + cmd + composer.getCmd()

    print "site composer done."
    print "insert now. this may take a while..."

    testresult = None

    try:
        #testresult = node.putraw(id, cmd + composer.getRawCmd())
        testresult = node.putraw2(id, cmd + composer.getRawCmd())
    except fcp.node.FCPException, e:
        print e

    node.shutdown()

    if testresult:
        print "success: " + testresult

        fms_host = ui.config('freenethg','fmshost')
        fms_port = ui.config('freenethg','fmsport')
        fms_user = ui.config('freenethg','fmsuser')
        fms_groups = ui.config('freenethg','fmsgroups')

        if fms_host and fms_port and fms_user and fms_groups:

            server = FMS_NNTP(fms_host, fms_user, fms_groups, int(fms_port))
            result = server.post_updatestatic(testresult)
            server.quit()

            print "NNTP result: %s" % str(result)


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
    ('', 'fcphost', None, 'specify fcphost if not 127.0.0.1'),
    ('', 'fcpport', None, 'specify fcpport if not 9481'),
]

cmdtable = {
       # cmd name        function call
       "fcp-bundle": (fcp_bundle,
                      [('f', 'force', None, 'run even when remote repository is unrelated'),
                       ('r', 'rev', [], 'a changeset you would like to bundle'),
                       ('', 'base', [], 'a base changeset to specify instead of a destination'),
                       ('', 'uri', None, 'use insert uri generate chk'),
                      ]+ remoteopts + fcpopts,
                      'hg fcp-bundle [--uri INSERTURI] [-f] [-r REV]... [--base REV]... [DEST]'),
       "fcp-unbundle": (fcp_unbundle,
                        [('u', 'update', None, 'update to new tip if changesets were unbundled'),
                         ] + fcpopts,
                        'hg fcp-unbundle [-u] FREENETKEY'),
       "fcp-makestatic": (fcp_makestatic,
                        [('', 'uri', None, 'use insert uri instead generate chk')
                         ] + fcpopts,
                        'hg fcp-makestatic [INSERTURI]'),
       #"fcp-createstatic": (fcp_createstatic,
       #                 [('a', 'auto', None, 'install update hook'),
       #                  ] + fcpopts,
       #                 'hg fcp-createstatic'),  
       "fcp-updatestatic": (fcp_updatestatic,
                        [] + fcpopts,
                        'hg fcp-updatestatic')
}
