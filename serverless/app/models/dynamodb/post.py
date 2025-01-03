import json
import secrets
import markdown
from app.common.date import utc_iso, iso_offset2utc
from app.common.string import new_uuid, nl2br, url2link, strip_html_tags
from app.common.dict import keys_from_dicts
from app.common.list import find_dicts
from app.common.util import float_to_decimal
from app.models.dynamodb.base import Base, ModelInvalidParamsException
from app.models.dynamodb.category import Category
from app.models.dynamodb.post_tag import PostTag
from app.models.dynamodb.service import Service
from app.models.dynamodb.file import File


class Post(Base):
    table_name = 'post'
    public_attrs = [
        'postId',
        'slug',
        'title',
        'body',
        'bodyFormat',
        'bodyHtml',
        'bodyText',
        'bodyJson',
        'postStatus',
        'publishAt',
        'updatedAt',
        'createdAt',
        'serviceId',
        'categorySlug',
        'categoryPath',  # Added
        'images',
        'files',
        'links',
        # 'isS3CacheCreated',
        # 's3CacheCreatedAt',
        'statusPublishAt',
    ]
    response_attrs = public_attrs + [
        'tags',
        'category',
    ]
    private_attrs = [
        'statusCategoryPathPublishAt',  # Added
        'createdBy',
        'previewToken',
        'isHiddenInList',
    ]
    all_attrs = public_attrs + private_attrs

    reserved_slugs = ['slug', 'groups']

    @classmethod
    def query_all(self, index_name, service_id, params, with_cate=False, is_public=True):
        table = self.get_table()
        status = params.get('status')
        until_time = params.get('untilTime', '')
        since_time = params.get('sinceTime', '')
        is_desc = params.get('order', 'asc') == 'desc'
        limit = params.get('count', 20)
        cate_slugs = params.get('categories', [])

        is_admin = index_name == 'createdAtGsi'
        sort_key = 'createdAt' if index_name == 'createdAtGsi' else 'publishAt'
        exp_attr_names = {}
        exp_attr_vals = {}
        key_conds = ['#si = :si']
        option = {
            'IndexName': index_name,
            'ProjectionExpression': self.prj_exps_str(is_public),
            # 'KeyConditionExpression': '#si = :si AND begins_with(#sp, :sp)',
            'ScanIndexForward': not is_desc,
            'Limit': limit,
        }
        exp_attr_names['#si'] = 'serviceId'
        exp_attr_vals[':si'] = service_id

        if not is_admin:
            status = 'publish'
            current = utc_iso(False, True)
            if not until_time or until_time > current:
                until_time = current

        if status:
            key_conds.append('begins_with(#sp, :sp)')
            exp_attr_names['#sp'] = 'statusPublishAt'
            exp_attr_vals[':sp'] = status

        filter_exps = []
        if since_time:
            cond = '#st > :st'
            exp_attr_names['#st'] = sort_key
            exp_attr_vals[':st'] = since_time
            if is_admin:
                key_conds.append(cond)
            else:
                filter_exps.append(cond)

        if until_time:
            cond = '#ut < :ut'
            exp_attr_names['#ut'] = sort_key
            exp_attr_vals[':ut'] = until_time
            if is_admin:
                key_conds.append(cond)
            else:
                filter_exps.append(cond)

        filter_exp_cids = []
        if cate_slugs:
            for i, cid in enumerate(cate_slugs):
                val_name = 'cid' + str(i)
                filter_exp_cids.append('#{v} = :{v}'.format(v=val_name))
                exp_attr_names[f'#{val_name}'] = 'categorySlug'
                exp_attr_vals[f':{val_name}'] = cid

        filter_exps_str = ' AND '.join(filter_exps) if filter_exps else ''
        filter_exp_cids_str = '(%s)' % ' OR '.join(
            filter_exp_cids) if filter_exp_cids else ''

        filter_exp = ''
        if filter_exps_str:
            filter_exp += filter_exps_str
        if filter_exp_cids_str:
            if filter_exp:
                filter_exp += ' AND '
            filter_exp += filter_exp_cids_str

        if filter_exp:
            option['FilterExpression'] = filter_exp
            option['Limit'] += 50

        option['KeyConditionExpression'] = ' AND '.join(key_conds)
        option['ExpressionAttributeNames'] = exp_attr_names
        option['ExpressionAttributeValues'] = exp_attr_vals
        result = table.query(**option)
        items = result.get('Items', [])[:limit]

        if with_cate:
            cate_slugs = keys_from_dicts(items, 'categorySlug')
            if cate_slugs:
                cates = {}
                for cate_slug in cate_slugs:
                    cates[cate_slug] = Category.get_one_by_slug(service_id, cate_slug,
                                                                False, False, True)
                for idx, item in enumerate(items):
                    cate = cates[item['categorySlug']]
                    items[idx]['category'] = cate
        return items

    @classmethod
    def get_filter_exps_for_pager_admin(self, exp_attr_names, exp_attr_vals, filter_conds=None):
        if filter_conds is None:
            filter_conds = {}

        filter_exps = []
        filter_exps_str = ''
        filters = filter_conds.get('filters', {})
        if filters and all(filters.values()):
            if filters['compare'] == 'contains':
                filter_exps_str = 'contains(#fattr, :fval)'

            elif filters['compare'] == 'eq':
                filter_exps_str = '#fattr = :fval'

            if filter_exps_str:
                filter_exps.append(filter_exps_str)
                exp_attr_names['#fattr'] = filters['attribute']
                exp_attr_vals[':fval'] = filters['value']

        # Filter for category
        exp_attr_names, exp_attr_vals, filter_exps = self.get_filter_exps_for_categories(
            exp_attr_names, exp_attr_vals, filter_exps, filter_conds.get('cate_slugs'))

        filter_exps_str_all = ' AND '.join(filter_exps)
        return exp_attr_names, exp_attr_vals, filter_exps_str_all

    @classmethod
    def get_filter_exps_for_pager_published(self, exp_attr_names, exp_attr_vals, filter_conds=None):
        if filter_conds is None:
            filter_conds = {}

        # current = utc_iso(False, True)
        # if not until_time or until_time > current:
        #    until_time = current

        filter_exps = []
        filter_exps_time = []
        # if since_time:
        #    cond = '#st > :st'
        #    exp_attr_names['#st'] = sort_key
        #    exp_attr_vals[':st'] = since_time
        #    if is_admin:
        #        key_conds.append(cond)
        #    else:
        #        filter_exps_time.append(cond)

        current = utc_iso(False, True)
        cond = '#ut < :ut'
        exp_attr_names['#ut'] = 'publishAt'
        exp_attr_vals[':ut'] = current
        filter_exps_time.append(cond)
        filter_exps_time_str = ' AND '.join(
            filter_exps_time) if filter_exps_time else ''
        if filter_exps_time_str:
            filter_exps.append(filter_exps_time_str)

        # Filter for category
        exp_attr_names, exp_attr_vals, filter_exps = self.get_filter_exps_for_categories(
            exp_attr_names, exp_attr_vals, filter_exps, filter_conds.get('cate_slugs'))

        filter_exps_str = ' AND '.join(filter_exps)
        return exp_attr_names, exp_attr_vals, filter_exps_str

    @classmethod
    def query_pager_admin(self, pkeys, params, pager_keys_def, index_name=None, filter_conds=None):
        is_desc = params.get('order', 'asc') == 'desc'
        limit = params.get('count', 20)
        start_key = params.get('pagerKey')

        option = {
            'IndexName': index_name,
            # 'ProjectionExpression': self.prj_exps_str(),
            'ScanIndexForward': not is_desc,
        }
        if index_name:
            option['IndexName'] = index_name

        key_conds = []
        exp_attr_names = {}
        exp_attr_vals = {}

        key_conds.append('#pk = :pk')
        exp_attr_names['#pk'] = pkeys['key']
        exp_attr_vals[':pk'] = pkeys['val']

        filter_exps_str = ''
        if filter_conds:
            exp_attr_names, exp_attr_vals, filter_exps_str =\
                self.get_filter_exps_for_pager_admin(
                    exp_attr_names, exp_attr_vals, filter_conds)

        if filter_exps_str:
            option['FilterExpression'] = filter_exps_str

        option['KeyConditionExpression'] = ' AND '.join(key_conds)
        option['ExpressionAttributeNames'] = exp_attr_names
        option['ExpressionAttributeValues'] = exp_attr_vals

        items, pager_key = self.query_loop_for_limit(option, limit, start_key,
                                                     pager_keys_def, len(filter_exps_str) > 0)
        return {
            'items': items,
            'pagerKey': pager_key
        }

    @staticmethod
    def get_filter_exps_for_categories(exp_attr_names, exp_attr_vals, filter_exps, cate_slugs):
        filter_exp_cids = []
        if cate_slugs:
            for i, cid in enumerate(cate_slugs):
                val_name = 'cid' + str(i)
                filter_exp_cids.append('#{v} = :{v}'.format(v=val_name))
                exp_attr_names[f'#{val_name}'] = 'categorySlug'
                exp_attr_vals[f':{val_name}'] = cid
        filter_exp_cids_str = '(%s)' % ' OR '.join(
            filter_exp_cids) if filter_exp_cids else ''
        if filter_exp_cids_str:
            filter_exps.append(filter_exp_cids_str)

        return exp_attr_names, exp_attr_vals, filter_exps

    @classmethod
    def get_one_by_id(self, post_id, with_relations=False, related_attrs_for_response=True):
        item = self.get_one_by_pkey('postId', post_id)
        if not item:
            return None

        if with_relations:
            if item.get('categorySlug'):
                item['category'] = Category.get_one_by_slug(item['serviceId'], item['categorySlug'],
                                                            True, False, related_attrs_for_response)
            item['tags'] = PostTag.get_all_by_post_id(
                post_id, True, related_attrs_for_response)

        return item

    @classmethod
    def query_all_by_tag_id(self, tag_id, params, with_cate=False, service_id=''):
        pkeys = {'key': 'tagId', 'val': tag_id}
        pager_keys = {'pkey': 'postId', 'index_pkey': 'tagId',
                      'index_skey': 'statusPublishAt'}
        res = PostTag.query_pager_published(
            pkeys, params, pager_keys, 'postsByTagGsi')
        items = res['items']
        new_items = []
        if items:
            keys = [{'postId': d['postId']} for d in items]
            posts = Post.batch_get_items(keys)

            if with_cate:
                posts = self.set_category_to_list(posts, service_id)

            for item in items:
                new = find_dicts(posts, 'postId', item['postId'])
                new['ori'] = item
                new_items.append(new)
            # is_desc = params.get('order', 'asc') == 'desc'
            # sort_key = 'publishAt'
        # return sorted(posts, key=lambda x: x[sort_key], reverse=is_desc)
        ret = {
            'items': new_items,
            'pagerKey': res['pagerKey'],
        }
        return ret

    @classmethod
    def get_one_by_slug(self, service_id, slug, with_cate=False, related_attrs_for_response=True):
        item = self.get_one_by_pkey('serviceIdSlug', '#'.join([service_id, slug]),
                                    True, 'serviceIdSlugGsi')
        if not item:
            return None

        if with_cate and item.get('categorySlug'):
            if item.get('categorySlug'):
                item['category'] = Category.get_one_by_slug(service_id, item['categorySlug'],
                                                            True, False, related_attrs_for_response)
            item['tags'] = PostTag.get_all_by_post_id(item['postId'], True,
                                                      related_attrs_for_response)

        return item

    @classmethod
    def check_exists_posts_related_with_category(self, service_id, cate_slug, category=None, count=5):
        if not category:
            category = Category.get_one_by_slug(
                service_id, cate_slug, True, True, False, False)
        if not category:
            raise ModelInvalidParamsException('Category not exists')

        filter_conds = {}
        cate_slugs = [cate_slug]
        if category['children']:
            for c in category['children']:
                cate_slugs.append(c['slug'])
        filter_conds['cate_slugs'] = cate_slugs

        vals = {'count': count}
        pkeys = {'key': 'serviceId', 'val': service_id}
        res = Post.query_pager_admin(
            pkeys, vals, None, 'createdAtGsi', filter_conds)
        return bool(res.get('items'))

    @classmethod
    def create(self, vals):
        service_id = vals.get('serviceId')
        if not service_id:
            raise ModelInvalidParamsException('serviceId is required')

        if not Service.check_exists(service_id):
            raise ModelInvalidParamsException('serviceId not exists')

        if vals['slug'] in self.reserved_slugs:
            raise ModelInvalidParamsException('This slug is not allowed')

        item = Post.get_one_by_slug(service_id, vals['slug'], False)
        if item:
            raise ModelInvalidParamsException(
                'Slug already used: ' + vals['slug'])

        cate = None
        if vals.get('category'):
            cate = Category.get_one_by_slug(service_id, vals['category'])
            if not cate:
                raise ModelInvalidParamsException('Category not exists')

        status = vals.get('status') or vals.get('postStatus')
        if status not in ['publish', 'unpublish']:
            raise ModelInvalidParamsException('status is invalid')
        is_publish = status == 'publish'

        time = utc_iso(False, True)

        created_at = ''
        if vals.get('createdAt'):
            created_at = vals.get('createdAt')
        else:
            created_at = time

        publish_at = vals.get('publishAt')
        if publish_at:
            publish_at = iso_offset2utc(publish_at, True)

        is_hidden = vals.get('isHiddenInList', False)

        sort_key_prefix, publish_at_key = self.get_sort_key_items(
            is_publish, is_hidden, publish_at, time)

        status_publish_at = '#'.join([sort_key_prefix, publish_at_key])

        cate_path = ''
        if cate:
            cate_path = '#'.join([cate['parentPath'], str(cate['id'])])
        status_cate_path = '_'.join(
            [sort_key_prefix, cate_path, publish_at_key])

        required_attrs = ['slug']
        for attr in required_attrs:
            if attr not in vals or len(vals[attr].strip()) == 0:
                raise ModelInvalidParamsException(
                    f"Argument '{attr}' requires values")

        slug = vals['slug']
        cate_slug = vals['category']
        token = vals['previewToken'] if vals.get(
            'previewToken') else secrets.token_hex()

        body_raw = vals['body']
        body_format = vals['bodyFormat']
        body_html, body_text, body_json = self.conv_body_to_each_format(
            body_raw, body_format)

        if vals.get('images'):
            file_ids = [file['fileId'] for file in vals['images']]
            File.bulk_update_status(file_ids, 'published')

        if vals.get('files'):
            file_ids = [file['fileId'] for file in vals['files']]
            File.bulk_update_status(file_ids, 'published')

        table = self.get_table()
        item = {
            'postId': new_uuid(),
            'createdAt': created_at,
            'createdBy': vals.get('createdBy'),
            'serviceId': service_id,
            'slug': slug,
            'publishAt': publish_at_key,
            'categorySlug': cate_slug,
            'categoryPath': cate_path,
            'statusCategoryPathPublishAt': status_cate_path,
            'title': vals['title'],
            'images': vals['images'],
            'files': vals['files'],
            'links': vals['links'],
            'previewToken': token,
            'body': body_raw,
            'bodyHtml': body_html,
            'bodyText': body_text,
            'bodyJson': body_json,
            'bodyFormat': body_format,
            'serviceIdSlug': '#'.join([service_id, slug]),
            'isHiddenInList': is_hidden,
            'postStatus': status,
            'statusPublishAt': status_publish_at,
            # 'isS3CacheCreated': False,
        }
        if vals.get('meta'):
            item['meta'] = vals.get('meta')

        if vals.get('updatedAt'):
            item['updatedAt'] = vals.get('updatedAt')

        table.put_item(Item=item)
        return item

    @classmethod
    def update(self, post_id, vals, is_update_time=True):
        time = utc_iso(False, True)
        saved = self.get_one_by_pkey('postId', post_id, True)
        if not saved:
            raise ModelInvalidParamsException('postId is invalid')

        service_id = saved['serviceId']

        slug_upd = vals.get('slug')
        if slug_upd and slug_upd != saved['slug']:
            if slug_upd in self.reserved_slugs:
                raise ModelInvalidParamsException('This slug is not allowed')

            if Post.get_one_by_slug(service_id, slug_upd, False):
                raise ModelInvalidParamsException('Slug already used')

        else:
            slug_upd = None

        cate_upd = None
        cate_slug_upd = vals.get('category')
        if cate_slug_upd is not None and cate_slug_upd != saved['categorySlug']:
            if cate_slug_upd:
                cate_upd = Category.get_one_by_slug(service_id, cate_slug_upd)
                if not cate_upd:
                    raise ModelInvalidParamsException(
                        'Category not exists', 400)
            else:
                cate_slug_upd = ''
        else:
            cate_slug_upd = None

        status_upd = vals.get('status')
        if status_upd is not None:
            if status_upd not in ['publish', 'unpublish']:
                raise ModelInvalidParamsException('status is invalid')

            if status_upd == saved['postStatus']:
                status_upd = None

        is_published = status_upd and status_upd == 'publish'

        publish_at_upd = vals.get('publishAt')
        if publish_at_upd:
            if publish_at_upd != saved['publishAt']:
                publish_at_upd = iso_offset2utc(vals['publishAt'], True)
        else:
            if is_published and (not saved['publishAt'] or saved['publishAt'] == 'None'):
                publish_at_upd = time

        is_hidden_upd = vals.get('isHiddenInList')
        if is_hidden_upd is not None:
            if is_hidden_upd == saved['isHiddenInList']:
                is_hidden_upd = None

        exp_items = []
        exp_vals = {}

        if slug_upd:
            exp_items.append('slug=:slug')
            exp_vals[':slug'] = slug_upd
            exp_items.append('serviceIdSlug=:sis')
            exp_vals[':sis'] = '#'.join([service_id, slug_upd])

        cate_path = ''
        if cate_slug_upd is not None:
            exp_items.append('categorySlug=:cates')
            exp_vals[':cates'] = cate_slug_upd

            exp_items.append('categoryPath=:catepath')
            if cate_upd:
                cate_path = '#'.join(
                    [cate_upd['parentPath'], str(cate_upd['id'])])
                exp_vals[':catepath'] = cate_path
            else:
                exp_vals[':catepath'] = ''

        if status_upd:
            exp_items.append('postStatus=:ps')
            exp_vals[':ps'] = status_upd

        if publish_at_upd:
            exp_items.append('publishAt=:pa')
            exp_vals[':pa'] = publish_at_upd

        if is_hidden_upd is not None:
            exp_items.append('isHiddenInList=:hil')
            exp_vals[':hil'] = is_hidden_upd

        is_upd_status_publish_at = False
        sort_key_prefix = None
        publish_at = saved['publishAt']

        status = status_upd if status_upd else saved['postStatus']
        is_publish = status == 'publish'
        is_hidden = is_hidden_upd if is_hidden_upd is not None else saved['isHiddenInList']
        publish_at = publish_at_upd if publish_at_upd else saved['publishAt']
        sort_key_prefix, publish_at_key = self.get_sort_key_items(
            is_publish, is_hidden, publish_at, time)

        if status_upd or publish_at_upd or is_hidden_upd is not None:
            upd_status_publish_at = '#'.join([sort_key_prefix, publish_at_key])
            if upd_status_publish_at != saved['statusPublishAt']:
                exp_items.append('statusPublishAt=:spa')
                exp_vals[':spa'] = upd_status_publish_at
                is_upd_status_publish_at = True

        if is_upd_status_publish_at or cate_slug_upd is not None:
            item_cate_path = cate_path if cate_slug_upd is not None else saved.get(
                'categoryPath')
            if item_cate_path:
                exp_items.append('statusCategoryPathPublishAt=:scpa')
                join_items = [sort_key_prefix, item_cate_path, publish_at_key]
                exp_vals[':scpa'] = '_'.join(join_items)

        saved_images = saved['images']
        upd_images = vals.get('images')
        del_img_fids = []
        add_img_fids = []
        if upd_images is not None and upd_images != saved_images:
            del_img_fids = [s['fileId']
                            for s in saved_images if s not in upd_images]
            add_img_fids = [s['fileId']
                            for s in upd_images if s not in saved_images]

        saved_files = saved['files']
        upd_files = vals.get('files')
        del_file_fids = []
        add_file_fids = []
        if upd_files is not None and upd_files != saved_files:
            del_file_fids = [s['fileId']
                             for s in saved_files if s not in upd_files]
            add_file_fids = [s['fileId']
                             for s in upd_files if s not in saved_files]

        attrs = ['title', 'body', 'bodyFormat', 'updatedBy', 'images',
                 'files', 'links']
        #  'files', 'links', 'isS3CacheCreated', 's3CacheCreatedAt']
        upd_attrs = []
        for attr in attrs:
            val = vals.get(attr)
            if val is None or val == saved.get(attr):
                continue

            exp_items.append('%s=:%s' % (attr, attr))
            exp_vals[':' + attr] = val
            upd_attrs.append(attr)

        if not exp_items:
            return {'item': None, 'is_updated_index': False}

        if 'body' in upd_attrs or 'bodyFormat' in upd_attrs:
            body_html, body_text, body_json = self.conv_body_to_each_format(
                vals['body'], vals['bodyFormat'])
            exp_items.append('%s=:%s' % ('bodyHtml', 'bodyHtml'))
            exp_items.append('%s=:%s' % ('bodyText', 'bodyText'))
            exp_items.append('%s=:%s' % ('bodyJson', 'bodyJson'))
            exp_vals[':bodyHtml'] = body_html
            exp_vals[':bodyText'] = body_text
            exp_vals[':bodyJson'] = body_json

        if is_update_time:
            updated_at = time
            exp_items.append('updatedAt=:ua')
            exp_vals[':ua'] = updated_at

        table = self.get_table()
        table.update_item(
            Key={
                'postId': post_id,
            },
            UpdateExpression='SET ' + ', '.join(exp_items),
            ExpressionAttributeValues=exp_vals,
            ReturnValues='UPDATED_NEW'
        )

        keys = {'postId': post_id}
        item = self.get_one_by_pkey_new(keys, True, True)

        if cate_slug_upd:
            item['category'] = cate_upd
        else:
            item['category'] = Category.get_one_by_slug(service_id, item['categorySlug'],
                                                        True, False, True)

        # Delete saved images
        if del_img_fids:
            File.bulk_update_status(del_img_fids, 'removed')

        # Add images
        if add_img_fids:
            File.bulk_update_status(add_img_fids, 'published')

        # Delete saved files
        if del_file_fids:
            File.bulk_update_status(del_file_fids, 'removed')

        # Add files
        if add_file_fids:
            File.bulk_update_status(add_file_fids, 'published')

        return {'item': item, 'is_updated_index': is_upd_status_publish_at}

    @staticmethod
    def get_sort_key_items(is_publish, is_hidden, publish_at, current_time=''):
        if not current_time:
            current_time = utc_iso(False, True)

        if is_publish and not publish_at:
            publish_at = current_time

        suffix = publish_at if publish_at else 'None'

        if not is_publish:
            return 'unpublish', suffix

        if publish_at > current_time:
            return 'reserve', suffix

        if is_hidden:
            return 'hidden', suffix

        return 'publish', suffix

    @staticmethod
    def conv_body_to_each_format(body_raw, body_format):
        body_html = ''
        body_text = ''
        body_json = None
        if body_format == 'markdown':
            body_raw = url2link(body_raw)
            extensions = ['extra', 'admonition', 'nl2br', 'sane_lists', 'toc']
            md = markdown.Markdown(safe_mode=True, extensions=extensions)
            body_html = md.convert(body_raw)
            body_text = strip_html_tags(body_html)
        elif body_format == 'text':
            body_html = nl2br(url2link(body_raw))
            body_text = body_raw
        elif body_format == 'json':
            body_json = float_to_decimal(json.loads(body_raw))
            # fmt: off
            body_html = f'<pre><code class="language-json">{body_raw}</code></pre>'
            # fmt: on
        else:
            body_html = body_raw
            body_text = strip_html_tags(body_raw)

        return body_html, body_text, body_json

    @staticmethod
    def set_category_to_list(posts, service_id):
        cate_slugs = keys_from_dicts(posts, 'categorySlug')
        if not cate_slugs:
            return posts

        cates = {}
        for cate_slug in cate_slugs:
            cates[cate_slug] = Category.get_one_by_slug(service_id, cate_slug,
                                                        False, False, True)
        for idx, post in enumerate(posts):
            cate = cates[post['categorySlug']]
            posts[idx]['category'] = cate

        return posts
