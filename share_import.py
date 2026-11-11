"""Allowlisted Chinese video share links and bounded redirect resolution."""
import re
from urllib import error, parse, request


LABELS = {'Bilibili':'빌리빌리','Douyin':'도우인','Xiaohongshu':'샤오홍슈','Kuaishou':'콰이쇼우'}
SHORT = {'b23.tv':'Bilibili','v.douyin.com':'Douyin','xhslink.com':'Xiaohongshu',
         'www.xhslink.com':'Xiaohongshu','v.kuaishou.com':'Kuaishou'}
HOSTS = {
    'Bilibili': {'www.bilibili.com','m.bilibili.com','bilibili.com'},
    'Douyin': {'www.douyin.com','douyin.com','m.douyin.com','www.iesdouyin.com','iesdouyin.com'},
    'Xiaohongshu': {'www.xiaohongshu.com','xiaohongshu.com','www.rednote.com','rednote.com'},
    'Kuaishou': {'www.kuaishou.com','kuaishou.com','m.kuaishou.com'},
}
CANONICAL = {
    'Bilibili': r'/video/(?:BV[0-9A-Za-z]{10}|av[0-9]{1,24})',
    'Douyin': r'/(?:share/)?video/[0-9]{8,24}',
    'Xiaohongshu': r'/(?:explore|discovery/item)/[0-9a-fA-F]{24}',
    'Kuaishou': r'/short-video/[0-9A-Za-z_-]{3,80}',
}
URL = re.compile(r'https?://[^\s<>"\u3000「」【】《》。，；！？]+',re.I)
RIGHTS_LIMIT = 500


def rights_note(value):
    if not isinstance(value,str) or len(value.strip())>RIGHTS_LIMIT:
        raise ValueError('사용 허락 근거는 500자 이내로 입력하세요.')
    return value.strip()


def classify(url):
    """Return platform, a safe request URL, and whether resolution is needed."""
    if len(url)>2048 or any(ord(c)<33 or c=='\\' for c in url):
        raise ValueError('지원하는 플랫폼의 영상 공유 링크를 입력하세요.')
    try:
        parsed=parse.urlsplit(url)
        if parsed.scheme not in ('http','https') or parsed.username is not None or parsed.password is not None or parsed.port is not None:
            raise ValueError()
    except ValueError:
        raise ValueError('지원하는 플랫폼의 영상 공유 링크를 입력하세요.') from None
    host=parsed.hostname
    path=parsed.path.rstrip('/')
    if host in SHORT and re.fullmatch(r'/(?:[amo]/)?[A-Za-z0-9_-]{3,100}',path):
        suffix='/' if host!='b23.tv' and parsed.path.endswith('/') else ''
        return SHORT[host], f'https://{host}{path}{suffix}', True
    for platform,hosts in HOSTS.items():
        if host not in hosts:
            continue
        if platform=='Kuaishou' and re.fullmatch(r'/f/[A-Za-z0-9_-]{3,100}',path):
            return platform,f'https://{host}{path}',True
        if platform=='Bilibili':
            path=re.sub(r'^/(?:zh-Hans|zh-Hant)(?=/video/)', '', path)
        if not re.fullmatch(CANONICAL[platform],path):
            break
        query=dict(parse.parse_qsl(parsed.query))
        kept={}
        if platform=='Bilibili' and re.fullmatch(r'[1-9][0-9]{0,3}',query.get('p','')):
            kept['p']=query['p']
        if platform=='Douyin':
            path=path.removeprefix('/share')
        if platform=='Xiaohongshu':
            kept={k:query[k] for k in ('xsec_token','xsec_source') if k in query}
        if platform=='Kuaishou' and query.get('shareObjectId') in (None,path.split('/')[-1]):
            # Keep only the navigation fields emitted by the share page.
            fields=('cc','kpf','kpn','shareMethod','subBiz','shareId','shareToken','source','shareMode','efid','shareObjectId')
            kept={key:query[key] for key in fields if re.fullmatch(r'[A-Za-z0-9_-]{1,100}',query.get(key,''))}
        domain=('www.rednote.com' if host in ('rednote.com','www.rednote.com') else
                {'Bilibili':'www.bilibili.com','Douyin':'www.douyin.com','Xiaohongshu':'www.xiaohongshu.com',
                 'Kuaishou':'www.kuaishou.com'}[platform])
        return platform,parse.urlunsplit(('https',domain,path,parse.urlencode(kept),'')),False
    raise ValueError('샤오홍슈·도우인·콰이쇼우·빌리빌리의 개별 영상 공유 링크를 입력하세요.')


def from_share(text):
    if not isinstance(text,str) or len(text)>4096:
        raise ValueError('공유 문구는 4096자 이내로 입력하세요.')
    found=set()
    for match in URL.finditer(text):
        try:
            found.add(classify(match.group().rstrip('.,!;:)]}')))
        except ValueError:
            continue
    if len(found)!=1:
        raise ValueError('지원하는 영상 공유 링크 한 개를 붙여넣으세요.')
    return found.pop()


def public_url(url,platform):
    parsed=parse.urlsplit(url)
    return parse.urlunsplit(parsed._replace(query='' if platform in ('Xiaohongshu','Kuaishou') else parsed.query))


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        return None


def resolve(platform,url,opener=None):
    """Follow short redirects on the same platform only; never follow arbitrary URLs."""
    opener=opener if opener is not None else request.build_opener(NoRedirect())
    for _ in range(6):
        target_platform,url,short=classify(url)
        if target_platform!=platform:
            raise ValueError('짧은 링크가 다른 플랫폼으로 이동했습니다.')
        if not short:
            return url
        try:
            with opener.open(request.Request(url,headers={'User-Agent':'Mozilla/5.0'}),timeout=10):
                raise ValueError('짧은 링크에서 영상 주소를 확인하지 못했습니다.')
        except error.HTTPError as exc:
            with exc:
                if exc.code not in (301,302,303,307,308) or not exc.headers.get('Location'):
                    raise ValueError('플랫폼이 짧은 링크 요청을 거절했습니다.') from None
                url=parse.urljoin(url,exc.headers['Location'])
        except (error.URLError,TimeoutError):
            raise ValueError('플랫폼에 연결하지 못했습니다.') from None
    raise ValueError('짧은 링크의 이동 경로가 너무 깁니다.')
