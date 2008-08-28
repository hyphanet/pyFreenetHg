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
 
from mercurial import hg
from mercurial import commands
from mercurial import repo,cmdutil,util,ui,revlog,node
from mercurial.node import bin

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

class _static_composer:
    """
    a helper class to compose the ClientPutComplexDir
    """
    #@    @+others
    #@+node:__init__
    def __init__(self, repo):
        """ """
        
        self._rootdir = repo.url()[5:] + '/.hg/'
        self._index = 0;
        self._fileitemlist = {}
        self._databuff = ''
        self._cmdbuff = ''
        
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
        
        self._databuff = self._databuff + content;
        idx = str(self._index)
            
        self._cmdbuff = self._cmdbuff + "Files."+idx+".Name=.hg/" + virtname + '\n'
        self._cmdbuff = self._cmdbuff + "Files."+idx+".UploadFrom=direct" + '\n'
        self._cmdbuff = self._cmdbuff + "Files."+idx+".Metadata.ContentType=text/plain" + '\n'
        self._cmdbuff = self._cmdbuff + "Files."+idx+".DataLength=" + str(len(content)) + '\n'    
        
        self._index = self._index + 1
        
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
     
     insertresult = node.put(data=bundledata, priority=1, mimetype="mercurial/bundle");
     
     node.shutdown();
     
     print "bundle inserted: " + insertresult
     

def fcp_unbundle(ui, repo, node, **opts):
     # The doc string below will show up in hg help
     """unbundle from CHK/USK (not implemented jet)"""
     #commands.unbundle(ui, repo, "Filename", **opts)

def _make_node(**opts):
    fcpopts = {}
    fcpopts['verbosity'] = fcp.INFO
    host = opts.get('fcphost', None)
    if host:
        fcpopts.put('host', host)
    port = opts.get('fcpport', None)
    if port:
        fcpopts.put('port', port)
    #return node2.FCPNode(**fcpopts)
    return myFCP(**fcpopts)
    
def fcp_makestatic(ui, repo, uri=None, **opts):
    """put the repo into freenet for access via static-http
    """
    
    id = "freenethgid" + str(int(time.time() * 1000000))
    if uri == None:
        uri="CHK@"
         
    cmd = "ClientPutComplexDir\n" + "URI=" + uri + "\nIdentifier=" + id
    cmd = cmd + "\nVerbosity=-1\nPriorityClass=1\n"
    
    composer = _static_composer(repo)   
    
    print "Debug: " + cmd + composer.getCmd()
    
    print "site composer done." 
    print "insert now. this may take a while..."
    
    node = _make_node(**opts)
    
    #testresult = node.putraw(id, cmd + composer.getRawCmd())
    testresult = node.putraw2(id, cmd + composer.getRawCmd())
    
    node.shutdown();
    
    print "success? " + testresult
    
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
    
    node = myFCP(**fcpopts)
         
    cmd = "ClientPutComplexDir\n" + "URI=" + uri + "\nIdentifier=" + id
    cmd = cmd + "\nVerbosity=-1\nPriorityClass=1\n"
    
    composer = _static_composer(repo)   
    
    print "Debug: " + cmd + composer.getCmd()
    
    print "site composer done." 
    print "insert now. this may take a while..."
    
    #testresult = 'debug'
    #testresult = node.putraw(id, cmd + composer.getRawCmd())
    testresult = node.putraw2(id, cmd + composer.getRawCmd())
    
    node.shutdown();
    
    print "success? " + testresult
    
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
