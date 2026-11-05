"""Conservative, explainable hash comparisons; never a deletion decision."""
import heapq
from itertools import combinations


def hash_library():
    try:
        import imagehash
    except ImportError:
        raise ValueError('相似检测需要 ImageHash，请安装 requirements.txt。') from None
    return imagehash


def dhash(gray):
    from PIL import Image
    small = gray.resize((9, 8), Image.Resampling.LANCZOS).tobytes()
    bits = 0
    for y in range(8):
        for x in range(8):
            bits = (bits << 1) | (small[y * 9 + x] > small[y * 9 + x + 1])
    return format(bits, '016x')


def phash(gray):
    return str(hash_library().phash(gray, hash_size=8, highfreq_factor=4))


def compare_pair(left, right, settings):
    """Apply identical eligibility gates to the baseline and candidate hash."""
    reasons = []
    if left.get('sha256') and left['sha256'] == right.get('sha256'):
        reasons.append('exact_duplicate')
    if min(left['luminance_std'], right['luminance_std']) < settings['min_luminance_std']:
        reasons.append('low_texture')
    # Cross products keep the inclusive boundary without subtracting float ratios.
    aspects = (left['width'] * right['height'], right['width'] * left['height'])
    numerator, denominator = settings['max_aspect_delta'].as_integer_ratio()
    if abs(aspects[0] - aspects[1]) * denominator > min(aspects) * numerator:
        reasons.append('aspect_ratio')
    if abs(left['mean_luminance'] - right['mean_luminance']) > settings['max_mean_delta']:
        reasons.append('luminance')
    d_distance = (int(left['dhash'], 16) ^ int(right['dhash'], 16)).bit_count()
    p_distance = (int(left['phash'], 16) ^ int(right['phash'], 16)).bit_count()
    d_match = not reasons and d_distance <= settings['near_distance']
    p_match = not reasons and p_distance <= settings['phash_distance']
    return {'a': left['name'], 'b': right['name'], 'eligible': not reasons,
            'exclusion_reasons': reasons, 'dhash_distance': d_distance,
            'phash_distance': p_distance, 'dhash_match': d_match,
            'phash_match': p_match, 'match': d_match and p_match}


def _priority(pair):
    rank = (0 if pair['match'] else 1 if pair['dhash_match'] or pair['phash_match']
            else 2 if pair['eligible'] else 3)
    return (rank, pair['dhash_distance'] + pair['phash_distance'],
            pair['dhash_distance'], pair['a'], pair['b'])


def algorithm_metadata(settings):
    return {'dhash': {'bits': 64, 'threshold': settings['near_distance'],
                      'implementation': 'horizontal-dhash-v1'},
            'phash': {'bits': 64, 'threshold': settings['phash_distance'],
                      'implementation': 'ImageHash.phash', 'hash_size': 8,
                      'highfreq_factor': 4},
            'decision': 'dhash_and_phash',
            'eligibility': 'symmetric-aspect-integer-ratio-v2',
            'image_evidence': 'nearest_prior_match'}


def similarity_report(images, settings, display_limit=200):
    """Compare every unique file pair; retain bounded, deterministic evidence."""
    unique = [item for item in images if item['decodable'] and not item['duplicate_of']]
    counts = dict.fromkeys(('compared_pairs', 'eligible_pairs', 'dhash_pairs',
                            'phash_pairs', 'near_duplicate_pairs'), 0)
    best_matches = {}

    def comparisons():
        for left, right in combinations(unique, 2):
            pair = compare_pair(left, right, settings)
            counts['compared_pairs'] += 1
            for field, key in (('eligible_pairs', 'eligible'), ('dhash_pairs', 'dhash_match'),
                               ('phash_pairs', 'phash_match'), ('near_duplicate_pairs', 'match')):
                counts[field] += int(pair[key])
            if pair['match']:
                priority = (pair['dhash_distance'] + pair['phash_distance'],
                            pair['dhash_distance'], pair['phash_distance'], left['name'])
                if right['name'] not in best_matches or priority < best_matches[right['name']][0]:
                    best_matches[right['name']] = (priority, pair)
            yield pair

    pairs = heapq.nsmallest(display_limit, comparisons(), key=_priority)
    for item in unique:
        match = best_matches.get(item['name'])
        if not match:
            continue
        pair = match[1]
        if 'near_duplicate' not in item['flags']:
            item['flags'].append('near_duplicate')
        item.update(similar_to=pair['a'], hash_distance=pair['dhash_distance'],
                    phash_distance=pair['phash_distance'])
    counts['displayed_pairs'] = len(pairs)
    return {'pairs': pairs, 'pair_sampling': {'strategy': 'all_unique_pairs',
            'display_limit': display_limit, 'truncated': counts['compared_pairs'] > len(pairs)},
            'algorithms': algorithm_metadata(settings),
            'pair_counts': counts}
