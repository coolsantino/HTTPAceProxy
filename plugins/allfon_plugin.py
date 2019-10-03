#-*- coding: utf-8 -*-
'''
AllFonTV Playlist Downloader Plugin
http://ip:port/allfon
'''

__author__ = 'AndreyPavlenko, Dorik1972'

import traceback
import gevent, requests, os
import logging, zlib
from urllib3.packages.six.moves.urllib.parse import urlparse, quote, unquote
from urllib3.packages.six import ensure_str, ensure_text, ensure_binary
from PlaylistGenerator import PlaylistGenerator
from requests_file import FileAdapter
from utils import schedule, query_get
import config.allfon as config
import config.picons.allfon as picons

class Allfon(object):

    handlers = ('allfon',)

    def __init__(self, AceConfig, AceProxy):
        self.logger = logging.getLogger('allfon_plugin')
        self.picons = self.channels = self.playlist = self.etag = self.last_modified = None
        self.playlisttime = gevent.time.time()
        self.headers = {'User-Agent': 'Magic Browser'}
        if config.updateevery: schedule(config.updateevery * 60, self.Playlistparser)

    def Playlistparser(self):
        try:
           s = requests.Session()
           s.mount('file://', FileAdapter())
           with s.get(config.url, headers=self.headers, proxies=config.proxies, stream=False, timeout=30) as r:
              if r.status_code != 304:
                 if r.encoding is None: r.encoding = 'utf-8'
                 self.headers['If-Modified-Since'] = gevent.time.strftime('%a, %d %b %Y %H:%M:%S %Z', gevent.time.gmtime(self.playlisttime))
                 self.playlist = PlaylistGenerator(m3uchanneltemplate=config.m3uchanneltemplate)
                 self.picons = picons.logomap
                 self.channels = {}
                 m = requests.auth.hashlib.md5()
                 self.logger.info('Playlist %s downloaded' % config.url)
                 pattern = requests.auth.re.compile(r',(?P<name>.+)[\r\n].+[\r\n].+[\r\n](?P<url>[^\r\n]+)?')
                 for match in pattern.finditer(r.text, requests.auth.re.MULTILINE):
                    itemdict = match.groupdict()
                    name = itemdict.get('name', '').replace(' (allfon)','')
                    url = itemdict['url']
                    itemdict['logo'] = self.picons[name] = itemdict.get('logo', picons.logomap.get(name))

                    if url.startswith(('acestream://', 'infohash://')) \
                         or (url.startswith(('http://','https://')) and url.endswith(('.acelive', '.acestream', '.acemedia', '.torrent'))):
                       self.channels[name] = url
                       itemdict['url'] = quote(ensure_str(name),'')

                    self.playlist.addItem(itemdict)
                    m.update(ensure_binary(name))

                 self.etag = '"' + m.hexdigest() + '"'
                 self.logger.debug('AllFon.m3u playlist generated')

              self.playlisttime = gevent.time.time()

        except requests.exceptions.RequestException: self.logger.error("Can't download %s playlist!" % config.url); return False
        except: self.logger.error(traceback.format_exc()); return False

        return True

    def handle(self, connection):
        # 30 minutes cache
        if not self.playlist or (gevent.time.time() - self.playlisttime > 30 * 60):
           if not self.Playlistparser(): connection.send_error()

        url = urlparse(connection.path)
        path = url.path.rstrip('/')
        ext = query_get(connection.query, 'ext', 'ts')
        if path.startswith('/{reqtype}/channel/'.format(**connection.__dict__)):
           if not path.endswith(ext):
              connection.send_error(404, 'Invalid path: %s' % unquote(path), logging.ERROR)
           name = ensure_text(unquote(os.path.splitext(os.path.basename(path))[0]))
           url = self.channels.get(name)
           if url is None:
              connection.send_error(404, 'Unknown channel: %s' % name, logging.ERROR)
           connection.__dict__.update({'channelName': name, 'reqtype_value': quote(url.split('/')[2],''), 'ext': ext, 'channelIcon': self.picons.get(name)})
           connection.__dict__.update({'path': {'acestream': lambda d: u'/content_id/{reqtype_value}/{channelName}.{ext}'.format(**d),
                                                'infohash' : lambda d: u'/infohash/{reqtype_value}/{channelName}.{ext}'.format(**d),
                                                'http'     : lambda d: u'/url/{reqtype_value}/{channelName}.{ext}'.format(**d),
                                                'https'    : lambda d: u'/url/{reqtype_value}/{channelName}.{ext}'.format(**d),
                                               }[urlparse(url).scheme](connection.__dict__)})
           connection.__dict__.update({'splittedpath': connection.path.split('/')})
           connection.__dict__.update({'reqtype': connection.splittedpath[1].lower()})
           return

        elif self.etag == connection.headers.get('If-None-Match'):
           self.logger.debug('ETag matches - returning 304')
           connection.send_response(304)
           connection.send_header('Connection', 'close')
           connection.end_headers()
           return

        else:
           hostport = connection.headers['Host']
           path = '' if not self.channels else '/{reqtype}/channel'.format(**connection.__dict__)
           exported = self.playlist.exportm3u(hostport=hostport, path=path, header=config.m3uheadertemplate, query=connection.query)
           response_headers = { 'Content-Type': 'audio/mpegurl; charset=utf-8', 'Connection': 'close', 'Content-Length': len(exported),
                                 'Access-Control-Allow-Origin': '*', 'ETag': self.etag }
           try:
              h = connection.headers.get('Accept-Encoding').split(',')[0]
              compress_method = { 'zlib': zlib.compressobj(9, zlib.DEFLATED, zlib.MAX_WBITS),
                                  'deflate': zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS),
                                  'gzip': zlib.compressobj(9, zlib.DEFLATED, zlib.MAX_WBITS | 16) }
              exported = compress_method[h].compress(exported) + compress_method[h].flush()
              response_headers['Content-Length'] = len(exported)
              response_headers['Content-Encoding'] = h
           except: pass

           connection.send_response(200)
           gevent.joinall([gevent.spawn(connection.send_header, k, v) for (k,v) in response_headers.items()])
           connection.end_headers()
           connection.wfile.write(exported)
           self.logger.debug('allfon.m3u playlist sent to {clientip}'.format(**connection.__dict__))
