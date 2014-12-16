from __future__ import division

import random
from collections import OrderedDict
from math import isnan
from os.path import join as pjoin
from os.path import exists
from os import mkdir
from array import array
from collections import namedtuple

import numpy
from pandas import DataFrame

from scipy.optimize import curve_fit

from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg as FigureCanvas

from vcf import Reader as pyvcfReader
from vcf import Writer as pyvcfWriter

from crumbs.iterutils import group_in_packets
from vcf_crumbs.iterutils import RandomAccessIterator

from vcf_crumbs.snv import VCFReader, VCFWriter, DEF_MIN_CALLS_FOR_POP_STATS
from vcf_crumbs.ld import _calc_recomb_rate

# Missing docstring
# pylint: disable=C0111
# Too few pulic methods
# pylint: disable=R0903

PASSED = 'passed'
FILTERED_OUT = 'filtered_out'

SNPS_PER_FILTER_PACKET = 50
DEF_SNV_WIN = 101
DEF_MIN_NUM_CHECK_SNPS_IN_WIN = 50
DEF_MAX_TEST_FAILURES = 5
DEF_MAX_FAILED_FREQ = DEF_MAX_TEST_FAILURES / DEF_SNV_WIN
DEF_MAX_DIST = 1000000
DEF_MIN_DIST = 125000

DEF_NUM_SNPS_IN_WIN_FOR_WEIRD_RECOMB = 51

def group_in_filter_packets(items, items_per_packet):
    for packet in group_in_packets(items, items_per_packet):
        yield {PASSED: packet, FILTERED_OUT: []}


def _write_log(log_fhand, tot_snps, passed_snps):
    log_fhand.write('SNVs processed: ' + str(tot_snps) + '\n')
    good_snps = sum(passed_snps.values())
    msg = 'SNVs passsed: ' + str(good_snps) + '\n'
    log_fhand.write(msg)
    msg = 'SNVs filtered out: ' + str(tot_snps - good_snps) + '\n'
    log_fhand.write(msg)
    log_fhand.write('Number of SNVs that passed each filter\n')
    for filter_, count in passed_snps.items():
        msg = filter_ + ': ' + str(count) + '\n'
        log_fhand.write(msg)
    log_fhand.flush()


def filter_snvs(in_fhand, out_fhand, filters, filtered_fhand=None,
                template_fhand=None, log_fhand=None, reader_kwargs=None):
    if reader_kwargs is None:
        reader_kwargs = {}
    reader = VCFReader(in_fhand, **reader_kwargs)

    template_reader = reader if template_fhand is None else VCFReader(template_fhand)
    writer = VCFWriter(out_fhand, template_reader=template_reader)
    if filtered_fhand:
        filtered_writer = VCFWriter(filtered_fhand,
                                    template_reader=template_reader)
    else:
        filtered_writer = None

    packets = group_in_filter_packets(reader.parse_snvs(),
                                      SNPS_PER_FILTER_PACKET)
    tot_snps = 0
    passed_snps = OrderedDict()
    for packet in packets:
        tot_snps += len(packet[PASSED]) + len(packet[FILTERED_OUT])
        for filter_ in filters:
            packet = filter_(packet)
            filter_name = filter_.__class__.__name__
            if filter_name not in passed_snps:
                passed_snps[filter_name] = 0
            passed_snps[filter_name] += len(packet[PASSED])

        for snv in packet[PASSED]:
            writer.write_snv(snv)
        if filtered_writer:
            for snv in packet[FILTERED_OUT]:
                filtered_writer.write_snv(snv)

    if log_fhand:
        _write_log(log_fhand, tot_snps, passed_snps)

    writer.flush()


class _BaseFilter(object):
    def __init__(self, samples_to_consider=None, reverse=False):
        self.reverse = reverse
        self.samples_to_consider = samples_to_consider

    def _setup_checks(self, filterpacket):
        pass

    def _do_check(self, snv):
        raise NotImplementedError()

    def __call__(self, filterpacket):
        self._setup_checks(filterpacket)
        reverse = self.reverse
        items_passed = []
        filtered_out = filterpacket[FILTERED_OUT][:]
        samples_to_consider = self.samples_to_consider
        for snv in filterpacket[PASSED]:
            if samples_to_consider is not None:
                snv_to_check = snv.filter_calls_by_sample(samples=samples_to_consider,
                                                          reverse=False)
            else:
                snv_to_check = snv

            passed = self._do_check(snv_to_check)
            if reverse:
                passed = not passed
            if passed:
                items_passed.append(snv)
            else:
                filtered_out.append(snv)

        return {PASSED: items_passed, FILTERED_OUT: filtered_out}


class MonomorphicFilter(_BaseFilter):
    "Filters monomorphic snvs"
    def __init__(self, reverse=False, samples_to_consider=None,
                 freq_threshold=1):
        parent_kwargs = {'reverse': reverse,
                         'samples_to_consider': samples_to_consider}
        super(MonomorphicFilter, self).__init__(**parent_kwargs)
        self._freq_threslhold = freq_threshold

    def _do_check(self, snv):
        return snv.is_polymorphic(self._freq_threslhold)


class CallRateFilter(_BaseFilter):
    'Filter by the min. number of genotypes called'

    def __init__(self, min_calls=None, min_call_rate=None, reverse=False,
                 samples_to_consider=None):
        parent_kwargs = {'reverse': reverse,
                         'samples_to_consider': samples_to_consider}
        super(CallRateFilter, self).__init__(**parent_kwargs)
        if min_calls is not None and min_call_rate is not None:
            msg = 'Both min_calls and min_call rate cannot be given'
            msg += 'at the same time'
            raise ValueError(msg)
        elif min_calls is None and min_call_rate is None:
            msg = 'min_calls or call_rate should be given'
            raise ValueError(msg)
        self.min_calls = min_calls
        self.min_call_rate = min_call_rate

    def _do_check(self, snv):
        if self.min_calls:
            if snv.num_called >= self.min_calls:
                return True
            else:
                return False
        else:
            if snv.call_rate >= self.min_call_rate:
                return True
            else:
                return False


class BiallelicFilter(_BaseFilter):
    'Filter the biallelic SNPs'

    def __init__(self, reverse=False, samples_to_consider=None):
        parent_kwargs = {'reverse': reverse,
                         'samples_to_consider': samples_to_consider}
        super(BiallelicFilter, self).__init__(**parent_kwargs)

    def _do_check(self, snv):
        if len(snv.alleles) == 2:
            return True
        else:
            return False


class IsSNPFilter(_BaseFilter):
    def _do_check(self, snv):
        return snv.is_snp


class SnvQualFilter(_BaseFilter):
    def __init__(self, min_qual, reverse=False, samples_to_consider=None):
        parent_kwargs = {'reverse': reverse,
                         'samples_to_consider': samples_to_consider}
        super(SnvQualFilter, self).__init__(**parent_kwargs)
        self.min_qual = min_qual

    def _do_check(self, snv):
        qual = snv.qual
        if qual is None:
            return False
        else:
            return qual >= self.min_qual


class ObsHetFilter(_BaseFilter):
    def __init__(self, min_het=None, max_het=None, remove_nd=True,
                 reverse=False, samples_to_consider=None):
        parent_kwargs = {'reverse': reverse,
                         'samples_to_consider': samples_to_consider}
        super(ObsHetFilter, self).__init__(**parent_kwargs)
        self.min_het = min_het
        self.max_het = max_het
        self.remove_nd = remove_nd

    def _do_check(self, snv):
        min_het = self.min_het
        max_het = self.max_het
        het = snv.obs_het
        if het is None and self.remove_nd:
            return False
        if min_het is not None and het < min_het:
            return False
        if max_het is not None and het > max_het:
            return False
        return True


class MafFilter(_BaseFilter):
    def __init__(self, min_maf=None, max_maf=None, remove_nd=True,
                 reverse=False, samples_to_consider=None):
        parent_kwargs = {'reverse': reverse,
                         'samples_to_consider': samples_to_consider}
        super(MafFilter, self).__init__(**parent_kwargs)
        if min_maf is None and max_maf is None:
            msg = 'At least one value should be given for the min or max het'
            raise ValueError(msg)
        self.min_maf = min_maf
        self.max_maf = max_maf
        self.remove_nd = remove_nd

    def _do_check(self, snv):
        min_maf = self.min_maf
        max_maf = self.max_maf
        maf = snv.maf
        if maf is None and self.remove_nd:
            return False
        if min_maf is not None and maf < min_maf:
            return False
        if max_maf is not None and maf > max_maf:
            return False
        return True

FISHER_CACHE = {}


def _fisher_extact_rxc(counts_obs, counts_exp):
    if (counts_obs, counts_exp) in FISHER_CACHE:
        return FISHER_CACHE[(counts_obs, counts_exp)]
    import rpy2.robjects as robjects
    env = robjects.r.baseenv()
    env['obs'] = robjects.IntVector(counts_obs)
    env['expected'] = robjects.IntVector(counts_exp)
    pvalue = robjects.r('fisher.test(cbind(obs, expected))$p.value')[0]

    FISHER_CACHE[(counts_obs, counts_exp)] = pvalue
    return pvalue


def filter_snvs_by_non_consistent_segregation(vcf_fpath, alpha=0.01,
                       yield_complete_info=False, num_snvs_check=DEF_SNV_WIN,
                       max_failed_freq=DEF_MAX_FAILED_FREQ,
                       max_test_failures=DEF_MAX_TEST_FAILURES,
                       win_width=DEF_MAX_DIST, win_mask_width=DEF_MIN_DIST,
                       min_num_snvs_check_in_win=DEF_MIN_NUM_CHECK_SNPS_IN_WIN,
                       min_samples=DEF_MIN_CALLS_FOR_POP_STATS):

    # We're assuming that most snps are ok and that a few have a weird
    # segregation
    if win_mask_width < 1:
        msg = 'You should mask at least a window of 3 bp to avoid the '
        msg += 'comparison with itself'
        raise ValueError(msg)
    reader = VCFReader(open(vcf_fpath), min_calls_for_pop_stats=min_samples)
    snvs = reader.parse_snvs()
    random_reader = VCFReader(open(vcf_fpath))
    if yield_complete_info:
        max_test_failures = None

    for snv_1 in snvs:
        loc = snv_1.pos
        win_1_start = loc - (win_width / 2)
        if win_1_start < 0:
            win_1_start = 0
        win_1_end = loc - (win_mask_width / 2)
        if win_1_end < 0:
            win_1_end = 0
        if win_1_end != 0:
            snvs_win_1 = random_reader.fetch_snvs(snv_1.chrom,
                                                  start=int(win_1_start),
                                                  end=int(win_1_end))
        else:
            snvs_win_1 = []

        win_2_start = loc + (win_mask_width / 2)
        win_2_end =  loc + (win_width / 2)
        snvs_win_2 = random_reader.fetch_snvs(snv_1.chrom,
                                              start=win_2_start,
                                              end=win_2_end)
        snvs_in_win = list(snvs_win_1) + list(snvs_win_2)
        if len(snvs_in_win) > num_snvs_check:
            snvs_in_win = random.sample(snvs_in_win, num_snvs_check)
        if len(snvs_in_win) < min_num_snvs_check_in_win:
            # Not enough snps to check
            continue

        exp_cnts = snv_1.biallelic_genotype_counts
        if exp_cnts is None:
            continue

        results = {'left': [], 'right':[]}
        values = {'left': [], 'right':[]}
        provisional_failures = 0
        failed = False
        for snv_2 in snvs_in_win:
            location = 'left' if snv_2.pos - loc < 0 else 'right'
            obs_cnts = snv_2.biallelic_genotype_counts
            if obs_cnts is None:
                continue
            value = _fisher_extact_rxc(obs_cnts, exp_cnts)
            result = False if value is None else value > alpha
            results[location].append(result)
            values[location].append((snv_2.pos, value))

            if result:
                provisional_failures += 1
                if (max_test_failures and
                    provisional_failures >= max_test_failures):
                    failed = True
                    break
        if failed:
            # too many snps are rejected even without bonferroni correction
            continue
        if (len(results['left']) + len(results['right']) <
                min_num_snvs_check_in_win):
            # few snps can be tested for segregation
            continue

        n_failed_left = results['left'].count(False)
        n_failed_right = results['right'].count(False)
        tot_checked = len(results['left']) + len(results['right'])
        if tot_checked > 0:
            failed_freq = (n_failed_left + n_failed_right) / tot_checked
            passed = max_failed_freq > failed_freq
        else:
            failed_freq = None
            passed = False
        if yield_complete_info:
            yield snv_1, {'results': results, 'values': values,
                          'failed_freq': failed_freq,
                          'n_failed': {'left': n_failed_left,
                                       'right': n_failed_right},
                          'checked': {'left': len(results['left']),
                                      'right': len(results['right'])},
                          'passed': passed}
        else:
            if passed:
                yield snv_1


def _get_calls(snv, samples):
    if samples is None:
        calls = snv.calls
    else:
        calls = [call for call in snv.calls if call.sample in samples]
    return calls


RecombRate = namedtuple('RecombRate', ['index_in_vcf', 'pos', 'recomb_rate'])


def _calculate_segregation_rates(snvs, pop_type, snps_in_window,
                                 samples=None):
    recomb_cache = {}
    half_win = (snps_in_window - 1) // 2
    for index1, snp1 in enumerate(snvs):
        calls1 = _get_calls(snp1, samples)
        start = index1 - half_win
        if start < 0:
            start = 0
        rates = []
        chrom = snp1.chrom
        for index2 in range(start, index1 + half_win):
            try:
                snp2 = snvs[index2]
            except IndexError:
                continue
            if chrom != snp2.chrom:
                continue
            calls2 = _get_calls(snp2, samples)
            index = tuple(sorted([index1, index2]))
            if index1 == index2:
                recomb_rate = 0
            try:
                recomb_rate = recomb_cache[index]
            except KeyError:
                recomb_rate = _calc_recomb_rate(calls1, calls2, pop_type)
                if recomb_rate is None:
                    recomb_rate = float('nan')
                else:
                    recomb_rate = recomb_rate[0]
                recomb_cache[index] = recomb_rate
            rates.append(RecombRate(index2, snp2.pos, recomb_rate))
        yield snp1, chrom, snp1.pos, rates


def filter_snvs_weird_recomb(snvs, pop_type, max_zero_dist_recomb,
                             snps_in_window=DEF_NUM_SNPS_IN_WIN_FOR_WEIRD_RECOMB,
                             min_num_snps=20, max_recomb_curve_fit=0.25,
                             debug_plot_dir=None):

    # TODO Add the tests
    # TODO User 95% confidence interval to see if 0 is reasonable?
    # http://kitchingroup.cheme.cmu.edu/blog/2013/02/12/Nonlinear-curve-fitting-with-parameter-confidence-intervals/

    snps = RandomAccessIterator(snvs, rnd_access_win=snps_in_window)
    rates = _calculate_segregation_rates(snps, pop_type, snps_in_window)
    for snp, chrom, pos, rates in rates:
        dists, recombs = zip(*[(rate.pos - pos, rate.recomb_rate) for rate in rates])
        if len(dists) < min_num_snps:
            continue
        if debug_plot_dir is None:
            plot_fhand = None
        else:
            chrom_dir = pjoin(debug_plot_dir, chrom)
            if not exists(chrom_dir):
                mkdir(chrom_dir)
            plot_fhand = open(pjoin(chrom_dir, str(pos) + '.png'), 'w')
        recomb_at_0 = _calc_ajusted_recomb(dists, recombs,
                                           max_recomb_curve_fit,
                                           plot_fhand=plot_fhand)

        # TODO find the method to decide the thresholds
        if recomb_at_0 < max_zero_dist_recomb:
            yield snp


def _kosambi(phys_dist, phys_gen_dist_conversion, recomb_at_origin):
    phys_gen_dist_conversion = abs(phys_gen_dist_conversion)
    # recomb rate should be in morgans per base
    d4 = numpy.absolute(phys_dist) * phys_gen_dist_conversion * 4
    return 0.5 * (numpy.exp(d4) - 1)/ (numpy.exp(d4) + 1) + recomb_at_origin


def _fit_kosambi(dists, recombs, init_params):
    try:
        return curve_fit(_kosambi, dists, recombs, p0=init_params)[0]
    except RuntimeError:
        return


def _print_figure(axes, figure, plot_fhand):
    if figure is None:
        return
    axes.legend()
    canvas = FigureCanvas(figure)
    canvas.print_figure(plot_fhand)


def _calc_ajusted_recomb(dists, recombs, max_recomb, plot_fhand=None):
    # first rough interpolation
    # we remove the physical distances with high recombination rates because
    # they're not very informative. e.g. more than 40 cM will not discriminate
    # between false recombination due to hidden segregation in the parents and
    # true recombination

    if plot_fhand:
        fig = Figure()
        axes = fig.add_subplot(111)
        axes.scatter(dists, recombs, c='r', label='All recombs')
    else:
        axes = None
        fig = None

    dists = numpy.array(dists)
    recombs = numpy.array(recombs)
    recomb_rate = 1e-7
    popt = _fit_kosambi(dists, recombs, init_params=[recomb_rate, 0])
    if popt is None:
        _print_figure(axes, fig, plot_fhand)
        return float('nan')

    est_dists = dists
    est_recombs = _kosambi(est_dists, popt[0], popt[1])

    #now we perform a second fit but only with those markers that are a
    #distance that results in a recombination fraction lower than max_recomb
    close_markers = est_recombs < max_recomb
    close_recombs = recombs[close_markers]
    close_dists = dists[close_markers]

    if len(close_dists) < 1:
        # This marker is so bad that their closest markers are at a big
        # distance
        _print_figure(axes, fig, plot_fhand)
        return float('nan')

    popt = _fit_kosambi(close_dists, close_recombs, init_params=popt)
    if popt is None:
        _print_figure(axes, fig, plot_fhand)
        return float('nan')

    est_close_recombs = _kosambi(close_dists, popt[0], popt[1])

    residuals = close_recombs - est_close_recombs
    if fig:
        axes.plot(close_dists, est_close_recombs, label='1st_fit')

    # we exclude the markers with a residual outlier
    quartile_25, quartile_75 = numpy.percentile(residuals, [25 ,75])
    iqr = quartile_75 - quartile_25
    outlayer_thrld = [quartile_25 - iqr * 1.5, quartile_75 + iqr * 1.5]
    ok_markers = ((outlayer_thrld[0] < residuals) &
                  (residuals < outlayer_thrld[1]))
    ok_recombs = close_recombs[ok_markers]
    ok_dists = close_dists[ok_markers]

    if fig:
        axes.scatter(ok_dists, ok_recombs, c='b', label='OK recombs')

    popt = _fit_kosambi(ok_dists, ok_recombs, init_params=popt)
    if popt is None:
        _print_figure(axes, fig, plot_fhand)
        return float('nan')

    est2_recombs = _kosambi(ok_dists, popt[0], popt[1])

    if fig:
        axes.plot(ok_dists, est2_recombs, label='2nd_fit')
        _print_figure(axes, fig, plot_fhand)

    return popt[1]
