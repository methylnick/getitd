import Bio.pairwise2 as bio
import pandas as pd
import numpy as np
import matplotlib as mpl
mpl.use('Agg')  # required to use matplotlib without X (via ssh + screen)
import matplotlib.pyplot as plt
import datetime
import collections
import os
import multiprocessing
import argparse


#######################################
## INITIALIZE VARIABLES

# prevent neg nkern/minBQS?
if False: #True:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    parser.add_argument("fastq1", help="FASTQ file of forward reads")
    parser.add_argument("fastq2", help="FASTQ file of reverse reads")
    parser.add_argument("sampleID", help="sample ID used as output folder prefix")
    parser.add_argument("minBQS", help="minimum average base quality score (BQS) required by each read (default: 30)", type=int, default=30, nargs='?')
    parser.add_argument("reference", help="WT amplicon sequence as reference for read alignment (default: /NGS/known_sites/hg19/flt3-itd_anno/amplicon.txt)", default="/NGS/known_sites/hg19/flt3-itd_anno/amplicon.txt", nargs='?')
    parser.add_argument('-nkern', help="number of cores to use for parallel tasks (default: 14)", default="4", type=int)
    parser.add_argument('-gap_open', help="alignment cost of gap opening", default="-20", type=int)
    parser.add_argument('-gap_extend', help="alignment cost of gap extension", default="-0.5", type=float)
    parser.add_argument('-match', help="alignment cost of base match", default="5", type=int)
    parser.add_argument('-mismatch', help="alignment cost of base mismatch", default="-4", type=int)
    parser.add_argument('-minscore', help="fraction of max possible alignment score required for ITD detection and insert collapsing", default="0.6", type=float)
    parser.add_argument('-known_length', help="file with expected ITD length, one on each line")
    group.add_argument('-known_vaf', help="file with expected ITD VAF (sum of all clones)")
    group.add_argument('-known_ar', help="file with expected ITD allele ratio (sum of all ITD clones vs WT)")
    cmd_args = parser.parse_args()
    
    R1 = cmd_args.fastq1
    R2 = cmd_args.fastq2
    SAMPLE = cmd_args.sampleID
    MIN_BQS = cmd_args.minBQS
    REF = cmd_args.reference
    NKERN = cmd_args.nkern
    KNOWN_LENGTH_FILE = cmd_args.known_length
    KNOWN_VAF_FILE = cmd_args.known_vaf
    KNOWN_AR_FILE = cmd_args.known_ar
    OUT_DIR = '_'.join([SAMPLE,'minBQS', str(MIN_BQS)])
    
    COST_MATCH = cmd_args.match
    COST_MISMATCH = cmd_args.mismatch
    COST_GAPOPEN = cmd_args.gap_open
    COST_GAPEXTEND = cmd_args.gap_extend
    MIN_SCORE = cmd_args.minscore
else:
    R1 = "1610-264-16KM1946-dx-a_S13_L001_R1_001.fastq"
    R2 = "1610-264-16KM1946-dx-a_S13_L001_R1_001.fastq"
    SAMPLE="264"
    MIN_BQS=35
    REF="/NGS/known_sites/hg19/flt3-itd_anno/amplicon.txt"
    NKERN=14
    KNOWN_LENGTH_FILE="264_known_length.txt"
    KNOWN_VAF_FILE="264-dx_known_vaf.txt"
    OUT_DIR = '_'.join([SAMPLE,'minBQS', str(MIN_BQS)])
    COST_MATCH = 5
    COST_MISMATCH = -10
    COST_GAPOPEN = -20
    COST_GAPEXTEND = -0.5
    MIN_SCORE=0.6




#######################################
## HELPER FUNCTIONS FOR PART 1 - PREPROCESSING

# read in txt file with known ITD length and VAF 
def read_known(filename, dtype=str):
    with open(filename) as f:
        return [dtype(x) for x in f.read().splitlines()]


# extract ITDs of known length from df  --> check that these are ints! (when best to fail if that's not the case?)
def get_known(df,known_length,sample=SAMPLE):
    df_found = df.ix[[x in known_length for x in df["length"]]]
    #
    # fill in available data on known ITDs/inserts that were missed (and not present in df)
    missed = [x for x in known_length if x not in list(df_found["length"])]
    df_missed = pd.DataFrame({"length": missed, "sample": [sample for x in range(len(missed))], "vaf": [0 for x in range(len(missed))], "counts": [0 for x in range(len(missed))]})
    #
    # concatenate known_found and known_missed
    df_known = pd.concat([df_found, df_missed])
    df_known[["length","counts"]] = df_known[["length","counts"]].astype(int)
    return df_known


# convert ITD allele ratio (AR) to variant allele frequency (VAF)
def ar_to_vaf(ar):
    return ar/(ar+1)


# read in FASTQ files, return reads and bqs
# --> possibly add saving to FASTQ file for all lines 
def read_fastq(filename):
    reads_and_bqs = []
    with open(filename,'r') as f:
        line = f.readline()
        while line:
            read_id = line
            read_seq = f.readline()
            read_desc = f.readline()
            read_bqs = f.readline()
            reads_and_bqs.append((read_seq.rstrip('\n'), read_bqs.rstrip('\n')))
            # read next line to see if there is one
            line = f.readline()
    return (reads_and_bqs)  


# filter reads based on average BQS 
def filter_bqs(args): 
    read, bqs = args[0]
    min_bqs = args[1]
    if sum([ord(x) -33 for x in bqs])/len(bqs) >= min_bqs:
        return read


# reverse complement and return a given read
def reverse_complement(read):
    return read.translate(str.maketrans('ATCGatcg','TAGCtagc'))[::-1]


# read in wt reference for alignment
def get_reference(filename):
    ref = None
    with open(filename, 'r') as f:
        ref = f.read()
    ref = ref.splitlines()
    assert len(ref) == 1
    return ref[0]


# get min score required to pass alignment score filter
def get_min_score(seq1, seq2, match_score=COST_MATCH, min_max_score_fraction=0.5):
    return min(len(seq1),len(seq2)) * match_score * min_max_score_fraction


# callback function to calculate match and mismatch score for realignment of insert to its read
# insert is masked by 'Z' -> return max penalty (min score of -Inf) to prohibit realignment of insert to itself
def get_alignment_score(char1,char2):
  assert not (char1 == 'Z' and char2 == 'Z') # only ever one of the sequences chars are taken from should contain masking letter 'Z', i.e. the read sequence but not the ref
  if char1 == char2:
    return COST_MATCH
  elif char1 == 'Z' or char2 == 'Z':
    return -np.inf
  else:
    return COST_MISMATCH

# align two sequences
# args must be a tuple because multiprocessing.pool can only pass one argument to the parallelized function!
def align(args):
    seq1, seq2 = args
    min_score = get_min_score(seq1, seq2)
    alignments = bio.align.globalcs(seq1, seq2, get_alignment_score, COST_GAPOPEN, COST_GAPEXTEND, penalize_end_gaps=False, one_alignment_only=True) # one alignment only until multiple ones are handled
    alignment_score = None
    if alignments != []:
        alignment_score = alignments[0][2]
        if alignment_score >= min_score:
            return alignments[0]
    return []


def connect_bases(char1, char2):
    if char1 == '-' or char2 == '-':
        return ' '
    if char1 == char2:
        return '|'
    return '.'


def connect_alignment(alignment):
    seq1, seq2 = alignment[0:2]
    return ''.join([connect_bases(char1,char2) for char1,char2 in zip(seq1,seq2)])


# count number of digits to align all lines in pretty alignment printout (need to know how much spaces to insert)
def get_number_of_digits(number):
    if number == 0: ## should never be 0!?
        return 1
    return int(np.log10(number)) +1


def print_alignment_connection(connection, pre_width,f):
    f.write(' ' * (pre_width +2))
    f.write(connection)
    f.write('\n')


def print_alignment_seq(seq, seq_coord, pre_width, post_width,f):
    f.write(' ' * (pre_width - get_number_of_digits(seq_coord) +1))
    f.write(str(seq_coord) + ' ')
    f.write(seq)
    seq_coord = seq_coord + len(seq) - seq.count('-') -1
    f.write(' ' * (post_width - get_number_of_digits(seq_coord)))
    f.write(str(seq_coord) + '\n')
    return seq_coord +1



# print pretty alignment, format inspired by EMBOSS needle output
def print_alignment(alignment, i, out_dir, command='bio.align.globalcs', command_seq='seq1', command_ref='seq2'):
    filename = 'needle_{}.txt'.format(i)
    seq, ref, score = alignment[0:3]
    al = connect_alignment(alignment)
    command_score_function = "get_alignment_score"
    cost_match = COST_MATCH
    cost_mismatch = COST_MISMATCH
    cost_gapopen = COST_GAPOPEN
    cost_gapextend = COST_GAPEXTEND
    width = 50
    pre_width = 20
    post_width = 7
    score_width = 15
    #
    with open(os.path.join(out_dir,filename), 'w') as f:
        f.write('########################################\n')
        f.write('# Program: Biopython\n')
        f.write('# Rundate: {}\n'.format(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%d")))
        f.write('# Commandline: {}(\n'.format(command))
        f.write('#    {},\n'.format(command_seq))
        f.write('#    {},\n'.format(command_ref))
        f.write('#    {},\n'.format(command_score_function))
        f.write('#    {},\n'.format(cost_gapopen))
        f.write('#    {})\n'.format(cost_gapextend))
        f.write('# Align_format: srspair\n')
        f.write('# Report_file: {}\n'.format(filename))
        f.write('########################################\n')
        f.write('\n')
        f.write('#=======================================\n')
        f.write('#\n')
        f.write('# Aligned_sequences: 2\n')
        f.write('# Sample: {}\n'.format(''.join([x for x in seq if x != '-'])))
        f.write('# Reference: {}\n'.format(''.join([x for x in ref if x != '-']).lower()))
        f.write('# Matrix: EDNAFULL\n')
        f.write('# Gap_penalty: {}\n'.format(cost_gapopen))
        f.write('# Extend_penalty: {}\n'.format(cost_gapextend))
        f.write('#\n')
        f.write('# Length: {}\n'.format(len(seq)))
        identity = '{}/{} ({}%)\n'.format(al.count('|'), len(seq), round(al.count('|')/len(seq) * 100,1))
        similarity = '{}/{} ({}%)\n'.format(al.count('|') + al.count('.'), len(seq), round((al.count('|') + al.count('.'))/len(seq) * 100,1))
        gaps = '{}/{} ({}%)\n'.format(len(seq) - al.count('|') - al.count('.'), len(seq), round((len(seq) - al.count('|') - al.count('.'))/len(seq) * 100,1))
        f.write('# Identity:     {}'.format(' ' * (score_width - len(identity)) + identity))
        f.write('# Similarity:   {}'.format(' ' * (score_width - len(similarity)) + similarity))
        f.write('# Gaps:         {}'.format(' ' * (score_width - len(gaps)) + gaps))
        f.write('# Score: {}\n'.format(score))
        f.write('#\n')
        f.write('#\n')
        f.write('#=======================================\n')
        f.write('\n')
        #
        # split alignment strings into per-line chunks for pretty printing
        alignment_chunks = [(seq[i:i+width],al[i:i+width],ref[i:i+width]) for i in range(0,len(seq),width)]
        seq_coord = 1
        ref_coord = 1
        for s,a,r in alignment_chunks:
            seq_coord = print_alignment_seq(s, seq_coord,pre_width,post_width,f) #better return string to write + seq_coord?
            print_alignment_connection(a, pre_width,f)
            ref_coord = print_alignment_seq(r, ref_coord,pre_width,post_width,f) 
            f.write('\n')
        #
        f.write('\n')
        f.write('#---------------------------------------\n')
        f.write('#---------------------------------------\n')


def parallelize(function, args, cores):
    with multiprocessing.Pool(cores) as p:
        return p.map(function, args)




#######################################
# MORE HELPER FUNCTIONS

# transform read coordinates to WT reference coords
def read_to_wt_coord(readn_coord, refn):
  wt_coord = readn_coord - sum(np.where(refn == '-')[0] < readn_coord)
  assert(wt_coord >= 0 and wt_coord < len(refn) - sum(refn == '-')) #wt_coord should be in the range of [0,len(wt_ref)[
  return wt_coord


# check that insert was realigned in one piece
def integral_insert_realignment(insert_alignment, insert_length):
    insert_idxs = [i for i in range(len(insert_alignment)) if insert_alignment[i] != '-']
    return insert_idxs[-1] - insert_idxs[0] +1 == insert_length


# check whether insert requires left normalization, i.e. has an ambiguous alignment and is not fully shifted to the left 
def left_normalize(readn, refn, insert_start, insert_end, i):
    if insert_start > 0 and insert_end < len(readn)-1 and refn[insert_start -1].lower() == readn[insert_end].lower():
        print("LEFT NORMALIZE: {}".format(i))
        print(readn)
        print(refn)
        return True
    return False


# filter inserts from a df supported by less than n unique_reads 
def filter_number_unique_reads(df, min_unique_reads):
    return df.loc[[len(x) > min_unique_reads for x in df["idx"]]]

# filter inserts from a df supported by less than n total reads
def filter_number_total_reads(df, min_total_reads):
    return df.loc[[x > min_total_reads for x in df["counts"]]]

# filter inserts from df that do not pass min_vaf
def filter_vaf(df, min_vaf):
    return df.loc[[x > min_vaf for x in df["vaf"]]]


#def filter_offset(df):   
#   df = df.iloc[[i for i in range(df.shape[0]) if df["trailing"][i] == True or df["offset"][i] == df["length"][i]]][['sample','file', 'idx', 'insert', 'length', start_col, 'counts','trailing','offset']]


def norm_start_coord(start, ref_wt):
    return min(max(0,start), len(ref_wt)-1)


# collapse inserts that have the same length, insert sequence and reference-based start coordinate
def collapse_same_inserts(df, start_col, ref_wt):
    # require that insert offset == insert length --> means they are adjacent   -> test this much earlier already, when saving inserts above! (left shift + extend first)
    if "offset" in df:
        df = df.iloc[[i for i in range(df.shape[0]) if df["trailing"][i] == True or df["offset"][i] == df["length"][i]]][['sample','file', 'idx', 'insert', 'length', start_col, 'counts','trailing','offset']]
        #df = df.ix[df["offset"] == df["length"]][['sample','file', 'idx', 'insert', 'length', start_col, 'counts']]
    df_grouped = df.groupby(by=['sample',"length",start_col,"insert"], as_index=False).sum()
    df_grouped["trailing"] = df.groupby(by=['sample',"length",start_col,"insert"], as_index=False).max()["trailing"]
    if 'offset' in df:    
        df_grouped["offset"] = df.groupby(by=['sample',"length",start_col,"insert"], as_index=False).max()["offset"]
    # 
    if start_col == "start":
        df_grouped["norm_start"] = [norm_start_coord(x, ref_wt) for x in df_grouped["start"]]
        df["norm_start"] = [norm_start_coord(x, ref_wt) for x in df["start"]]
        start_col = "norm_start"
    df_grouped["ref_coverage"] = [ref_coverage[pos] for pos in df_grouped[start_col]]
    df_grouped["vaf"] = df_grouped["counts"]/df_grouped["ref_coverage"] * 100
    df_grouped["file"] = np.zeros(len(df_grouped)) 
    df_grouped["counts_each"] = np.zeros(len(df_grouped)) 
    df_grouped[["idx","file","counts_each"]] = df_grouped[["idx","file","counts_each"]].astype("object")
    #
    for i in range(len(df_grouped)):
        this_df = df[np.array(df["length"] == df_grouped.ix[i,"length"]) * np.array(df[start_col] == df_grouped.ix[i,start_col]) * np.array(df["insert"] == df_grouped.ix[i,"insert"])]
        #this_df = df[np.array(df["length"] == df_grouped.ix[i,"length"]) * np.array(df[start_col] == df_grouped.ix[i,start_col]) * np.array(df["insert"] == df_grouped.ix[i,"insert"]) * np.array(df["trailing"] == df_grouped.ix[i,"trailing"])]
        df_grouped.set_value(i,"idx",this_df["idx"].tolist())
        df_grouped.set_value(i,"file",this_df["file"].tolist())
        df_grouped.set_value(i,"counts_each",[np.int(x) for x in this_df["counts"]])
    #
    # check that sum of "counts_each" (= read counts of each unique read) equals total counts in "counts"
    assert [sum(x) for x in df_grouped["counts_each"]] == [int(x) for x in df_grouped["counts"]]
    return df_grouped


def empty_df(start_col):
    empty_df = pd.DataFrame(columns=['length', 'trailing', start_col, 'insert', 'idx', 'file', 'counts', 'ref_coverage', 'vaf', 'counts_each','offset'])
    empty_df[["length",start_col,"counts","ref_coverage"]] = empty_df[["length",start_col,"counts","ref_coverage"]].astype("int64")
    empty_df["idx"] = []
    empty_df["file"] = []
    empty_df["counts_each"] = []
    return empty_df


# collapse inserts that have the same length and start coordinate and a SIMILAR insert sequence
def collapse_similar_inserts(df, start_col):
    df_collapsed = empty_df(start_col) # collect all collapsed inserts
    #
    for length in set(df["length"]):
        this_df_length = df.ix[df["length"] == length]
        #
        for start in set(this_df_length[start_col]):
            this_df = this_df_length.ix[this_df_length[start_col] == start]
            this_df_collapsed = empty_df(start_col)  # collect inserts of the same length and start-coord in this tmp df -> use this one for collapsing!
            #
            max_score = length * COST_MATCH # pass this as param?
            min_score = max_score * MIN_SCORE
            #
            for i in range(this_df.shape[0]):
                i_idx = this_df.index[i]
                this_ins = this_df["insert"][i_idx]
                collapsed = False
                #
                for ii,ii_row in this_df_collapsed[::-1].iterrows(): #[::-1] to reverse df and speed up pos alignment
                    other_ins = ii_row["insert"]
                    #
                    alignment = bio.align.globalcs(this_ins, other_ins, get_alignment_score, COST_GAPOPEN, COST_GAPEXTEND, one_alignment_only=True, penalize_end_gaps=True)[0] # one alignment only at least until multiple ones are handled
                    alignment_score = alignment[2]
                    #
                    if alignment_score >= min_score:
                        collapsed = True
                        # collapse
                        # add together some statistics
                        this_df_collapsed.at[ii,"counts"] = ii_row["counts"] + this_df["counts"][i_idx]
                        this_df_collapsed.at[ii, "vaf"] = ii_row["vaf"] + this_df["vaf"][i_idx]
                        this_df_collapsed.at[ii,"trailing"] = max(max(this_df["trailing"]), max(this_df_collapsed["trailing"]))
                        if 'offset' in df:
                            this_df_collapsed.at[ii,"offset"] = max(max(this_df["offset"]), max(this_df_collapsed["offset"]))
                        #
                        # pick one or the other for the others OR keep both but in specific order (first list for picked insert) -> go for the most abundant one (or the one closest to reference?!)
                        if this_df["counts"][i_idx] > ii_row["counts"]:
                            this_df_collapsed.at[ii, "insert"] = this_df["insert"][i_idx]
                            this_df_collapsed.at[ii, "ref_coverage"] = this_df["ref_coverage"][i_idx]
                            #this_df_collapsed.at[ii, "offset"] = this_df["offset"][i_idx]
                            #
                            this_df_collapsed.at[ii, "idx"] = this_df["idx"][i_idx] + ii_row["idx"]
                            this_df_collapsed.at[ii, "file"] = this_df["file"][i_idx] + ii_row["file"]
                            this_df_collapsed.at[ii, "counts_each"] = this_df["counts_each"][i_idx] + ii_row["counts_each"]
                        else:
                            this_df_collapsed.at[ii, "idx"] = ii_row["idx"] + this_df["idx"][i_idx]
                            this_df_collapsed.at[ii, "file"] = ii_row["file"] + this_df["file"][i_idx]
                            this_df_collapsed.at[ii, "counts_each"] = ii_row["counts_each"] + this_df["counts_each"][i_idx]
                        break
                #
                if not collapsed:
                    this_df_collapsed = this_df_collapsed.append(this_df.ix[i_idx], ignore_index=True)
            # append collapsed inserts of current length and start-coord to the overall collecting df
            df_collapsed = df_collapsed.append(this_df_collapsed, ignore_index=True)
    #
    # check that sum of "counts_each" (= read counts of each unique read) equals total counts in "counts"
    assert([sum(x) for x in df_collapsed["counts_each"]] == [int(x) for x in df_collapsed["counts"]])
    return df_collapsed


def index_of_max(lst):
    return lst.index(max(lst))


# collapse inserts that have the same length and SIMILAR insert sequence but no restrain on start coord!
def collapse_close_inserts(df, start_col):
    df_collapsed = empty_df(start_col) # collect all collapsed inserts
    #
    for length in set(df["length"]):
        this_df = df.ix[df["length"] == length]
        this_df_collapsed = empty_df(start_col)  # collect inserts of the same length and start-coord in this tmp df -> use this one for collapsing!
        #
        max_score = length * COST_MATCH # param?
        min_score = max_score * MIN_SCORE
        #
        for i in range(this_df.shape[0]):
            i_idx = this_df.index[i]
            this_max_counts = index_of_max(this_df["counts_each"][i_idx])
            this_read = all_reads[this_df["idx"][i_idx][this_max_counts]]
            this_ins_start = this_df[start_col][i_idx]
            collapsed = False
            #
            for ii,ii_row in this_df_collapsed[::-1].iterrows(): #[::-1] to reverse df and speed up pos alignment
                other_max_counts = index_of_max(ii_row["counts_each"])
                other_read = all_reads[ii_row["idx"][other_max_counts]]
                other_ins_start = ii_row[start_col]
                min_start = min(this_ins_start, other_ins_start)
                max_start = max(this_ins_start, other_ins_start)
                #
                alignments = bio.align.globalcs(this_read[min_start:max_start+length], other_read[min_start:max_start+length], get_alignment_score, COST_GAPOPEN, COST_GAPEXTEND, one_alignment_only=True, penalize_end_gaps=True)
                alignment_score = None
                if alignments == []:
                    alignment_score = -1
                else:
                    alignment_score = alignments[0][2]
                #
                if alignment_score >= min_score:
                    collapsed = True
                    # collapse
                    # add together some statistics
                    this_df_collapsed.at[ii,"counts"] = this_df_collapsed["counts"][ii] + this_df["counts"][i_idx]
                    this_df_collapsed.at[ii, "vaf"] = this_df_collapsed["vaf"][ii] + this_df["vaf"][i_idx]
                    this_df_collapsed.at[ii,"trailing"] = max(max(this_df["trailing"]), max(this_df_collapsed["trailing"]))
                    if 'offset' in df:
                        this_df_collapsed.at[ii,"offset"] = max(max(this_df["offset"]), max(this_df_collapsed["offset"]))
                    #
                    # pick one or the other for the others OR keep both but in specific order (first list for picked insert) -> go for the most abundant one (or the one closest to reference?!)
                    if this_df["counts"][i_idx] > ii_row["counts"]:
                        this_df_collapsed.at[ii, "insert"] = this_df["insert"][i_idx]
                        this_df_collapsed.at[ii, "ref_coverage"] = this_df["ref_coverage"][i_idx]
                        this_df_collapsed.at[ii, start_col] = this_df[start_col][i_idx]
                        #this_df_collapsed.at[ii, "offset"] = this_df["offset"][i_idx]
                        #
                        this_df_collapsed.at[ii, "idx"] = this_df["idx"][i_idx] + ii_row["idx"]
                        this_df_collapsed.at[ii, "file"] = this_df["file"][i_idx] + ii_row["file"]
                        this_df_collapsed.at[ii, "counts_each"] = this_df["counts_each"][i_idx] + ii_row["counts_each"]
                    else:
                        this_df_collapsed.at[ii, "idx"] = ii_row["idx"] + this_df["idx"][i_idx]
                        this_df_collapsed.at[ii, "file"] = ii_row["file"] + this_df["file"][i_idx]
                        this_df_collapsed.at[ii, "counts_each"] = ii_row["counts_each"] + this_df["counts_each"][i_idx]
                    break
            #
            if not collapsed:
                this_df_collapsed = this_df_collapsed.append(this_df.ix[i_idx], ignore_index=True)
        # append collapsed inserts of current length and start-coord to the overall collecting df
        df_collapsed = df_collapsed.append(this_df_collapsed, ignore_index=True)
    #
    # check that sum of "counts_each" (= read counts of each unique read) equals total counts in "counts"
    assert([sum(x) for x in df_collapsed["counts_each"]] == [int(x) for x in df_collapsed["counts"]])
    return df_collapsed



# collapse df  --> can I use this to simply the other collapsing methods?
# -> keep: columns containing the same value in all rows -> pick any 
# -> add: columns to sum up (such as total ITD VAF or counts)
# -> append: columns for which all rows should be collapsed to one entry with a single list
def collapse_all(df,add,append,keep):
    # keep -> columns with same value in all rows, just pick one and keep that
    assert df[keep].drop_duplicates().shape[0] == 1
    df_collapsed = pd.DataFrame()
    df_collapsed[keep] = pd.DataFrame(df.iloc[0][keep].values.tolist(), columns=keep)
    df_collapsed[add] = df.groupby(by=keep, as_index=False).sum()[add]
    for col in append:
        df_collapsed[col] = [df[col].values.tolist()]
    # keep track of which ITD contributed which counts/vaf to the "add" columns
    for col in add:
        df_collapsed[col + '_each'] = [df[col].values.tolist()]
    return df_collapsed



# filter ITDs
def filter_inserts(df):
    df = filter_number_unique_reads(df, 2)
    df = filter_number_total_reads(df, 30)
    df = filter_vaf(df, 0.001)
    return df


# update length of trailing ITDs to offset instead (should be max potential ITD length)
def fix_trailing_length(df):
    df_fixed = df.copy()
    df_fixed.loc[df["trailing"],"length"] = df_fixed.ix[df["trailing"],"offset"]
    return df_fixed.sort_values(['length','tandem2_start'])
    


#######################################
#######################################
## READ INPUT & CREATE OUTPUT FOLDERS
#######################################
#######################################

if __name__ == '__main__':
    #
    ## CREATE OUTPUT FOLDER
    if not os.path.exists(OUT_DIR):
        os.makedirs(OUT_DIR)
    #
    #
    ## GET READS FROM FASTQ 
    # collect reads that pass MIN_BQS filter -> reverse-complement R2 reads so that all reads can be aligned to the same reference
    reads_and_bqs = read_fastq(R1) + [(reverse_complement(r2_read),bqs) for r2_read,bqs in read_fastq(R2)]
    print("Number of total reads: {}".format(len(reads_and_bqs)))
    args = [(read_and_bqs, MIN_BQS) for read_and_bqs in reads_and_bqs]
    #
    # filter based on BQS -> PASS returns read, FAIL returns None -> remove None from list!
    reads = None
    if MIN_BQS > 0:
        reads = [x for x in parallelize(filter_bqs, args, NKERN) if x is not None]
    else:
        reads = [x[0] for x in reads_and_bqs]
    #
    #
    # get unique reads and counts thereof
    print("Number of total reads with mean BQS >= {}: {}".format(MIN_BQS,len(reads)))
    tmp = collections.Counter(reads)
    unique_reads = list(tmp.keys())
    unique_reads_counts = list(tmp.values())
    assert len(unique_reads) == len(unique_reads_counts)
    print("Number of unique reads with mean BQS >= {}: {}".format(MIN_BQS,len(unique_reads)))
    #
    #
    # filter unique reads -> keep only reads that exist at least twice  -----> make this assignment nicer, it's like the same loop twice!!?
    unique_reads, unique_reads_counts = [read  for read,count in zip(unique_reads, unique_reads_counts) if count >= 2], [count for read,count in zip(unique_reads, unique_reads_counts) if count >= 2]
    assert len(unique_reads) == len(unique_reads_counts)
    print("Number of unique reads present at least twice: {}".format(len(unique_reads)))
    print("--> Number of total reads passing both filters: {}".format(sum(unique_reads_counts)))
    #
    #
    ## GET REFERENCE
    # --> compare variable with those below (below it's ref_wt and list of chars instead of string?)
    wt_ref = get_reference(REF)
    wt_ref_upper = wt_ref.upper()
    #
    #
    #
    ## DO ALIGNMENTS & FILTER BASED ON ALIGNMENT SCORE
    all_alignments = None
    args = [(unique_read, wt_ref_upper) for unique_read in unique_reads]
    with multiprocessing.Pool(NKERN) as p:
            all_alignments = p.map(align, args)
    assert len(unique_reads) == len(all_alignments)
    #
    print("\nFiltering {} / {} low quality alignments with a score < {}".format(all_alignments.count([]),len(all_alignments),  "50 % of max"))
    #
    #
    all_readCounts  = [unique_reads_counts[i] for i in range(len(all_alignments)) if all_alignments[i] != []]
    all_alignments  = [x for x in all_alignments if x != []]
    all_reads       = [x[0] for x in all_alignments]
    all_refs        = [x[1] for x in all_alignments]
    all_scores      = [x[2] for x in all_alignments]
    all_files       = ['needle_{}.txt'.format(i) for i in range(len(all_alignments))]  ## --> do I even need this anymore?
    #
    #
    ## PRINT ALIGNMENTS
    # create output file directory for alignments print-outs
    needle_dir = os.path.join(OUT_DIR,'out_needle')
    if not os.path.exists(needle_dir):
        os.makedirs(needle_dir)
    #
    for i in range(len(all_alignments)):
        print_alignment(all_alignments[i], i, needle_dir, command='bio.align.globalcs', command_seq='unique_reads[i]', command_ref='wt_ref')
    #
    #
    # make sure there is a 1:1 matching between these files -> must all have the same length!  -> since I am reading out of all_alignments, this should always be the case?
    assert(len(all_reads) == len(all_readCounts))
    assert(len(all_reads) == len(all_alignments))
    assert(len(all_reads) == len(all_refs))
    assert(len(all_reads) == len(all_scores))
    assert(len(all_reads) == len(all_files))
    #
    #
    #######################################
    # EXTRACT INSERT SEQUENCE FROM READ
    #
    # check each alignment for insert/itd and save index in all_reads/all_refs/all_files to idx, insert/itd length to length and insert/itd start/stop position to start/end dicts based on insert/itd classification
    w_ins = {"idx": [], "file": [], "length": [], "start": [], "insert": [], "trailing": []}
    w_itd_exact = {"idx": [], "file": [], "length": [], "start": [], "tandem2_start": [], "offset": [], "insert": [], "trailing": []}
    w_itd_nonexact_fail = {"idx": [], "file": [], "length": [], "start": [], "insert": [], "trailing": []}
    w_itd_nonexact = {"idx": [], "file": [], "length": [], "start": [], "tandem2_start": [], "offset": [], "insert": [], "trailing": []}
    #
    ref_wt = [base for base in all_refs[0] if base != '-'] 
    ref_coverage = np.zeros(len(ref_wt)) # count number of reads covering each bp AND its successor (therefore do not calc coverage for last bp)
    #
    ambig_i = []
    ambig_als = []
    should_left_normalize = 0
    #
    # loop over all alignments, test for presence of an ITD
    for read,ref,score,counts,filename,i in zip(all_reads, all_refs, all_scores, all_readCounts, all_files, range(len(all_reads))):
        readn = np.array(list(read))
        refn = np.array(list(ref))
        assert(len(readn) == len(refn))
#
        readn_onRef = readn[refn != '-'] ## compare readn_nonIns below
        readn_onRef_covered = np.where(readn_onRef != '-')
        readn_onRef_covered_range = np.arange(np.min(readn_onRef_covered), np.max(readn_onRef_covered)) # do not count last index -> read ending here holds no information on whether or not an ITD starts here --> but what about a read that covers the first 5 bases of an ITD > 5bp, I still wouldn't know...
        ref_coverage[readn_onRef_covered_range] = ref_coverage[readn_onRef_covered_range] + counts
#	
        # get indeces of inserts in read (positions where reference has a gap and read does not)
        insert_idxs_all = np.arange(len(readn))[refn == '-']
        assert('-' not in readn[insert_idxs_all]) # two gaps should never align at the same pos!
#	
        # get indeces of each individual insert in each read
        insert_idxs_each = []
        insert_idxs_tmp = []
        i_prev = None
        for i_this in insert_idxs_all:
            if i_prev is None or i_prev == i_this -1: #start saving first/continue saving next insert index
                insert_idxs_tmp.append(i_this)
                i_prev = i_this
            else: #save current insert_idxs_tmp list and open up a new one for the next insert
                insert_idxs_each.append(insert_idxs_tmp)
                insert_idxs_tmp = [i_this]
                i_prev = i_this
        # save last insert as well
        insert_idxs_each.append(insert_idxs_tmp)
        assert(np.all(np.concatenate(insert_idxs_each) == insert_idxs_all))		
#
        for insert_idxs in insert_idxs_each:
            insert_length = len(insert_idxs)	
            if insert_length < 6 or "N" in readn[insert_idxs]: # tested again below...
                continue
            ins = readn[insert_idxs]
            insert_start = insert_idxs[0]
            insert_end = insert_idxs[-1]
            trailing = insert_start == 0 or insert_end == sum(readn != '-')-1
            # if there is an insert  --> require min 6 bp length, in-frame insert and no "N"s within insert
            if(insert_length >= 6 and "N" not in readn[insert_idxs] and (trailing or insert_length % 3 == 0)):
                if insert_start > 0:
                    should_left_normalize = should_left_normalize + left_normalize(readn, refn, insert_start, insert_end, i)
#
                # relative to the reference, get coord of the first WT base before insert	
                insert_start_ref = insert_start - sum(refn[0:insert_start] == '-')  
                if insert_start == 0: 
                    insert_start_ref = insert_start_ref - insert_length
#	    
                w_ins["idx"].append(i)
                w_ins["file"].append(filename)
                w_ins["length"].append(insert_length)
                w_ins["start"].append(insert_start_ref)
                w_ins["insert"].append(''.join(ins))
                w_ins["trailing"].append(trailing)
#			
                # check whether the insert is contained within non-insert read a second time -> that makes it an ITD!
                readn_nonIns = np.delete(readn,insert_idxs)
                readn_maskedIns = readn.copy()
                readn_maskedIns[insert_idxs] = 'Z' # wild base for "no base" -> prevent alignment to already detected insert
#
                # search for nearest tandem before and after ITD
                tandem2_after = ''.join(readn_maskedIns).find(''.join(ins), insert_start + insert_length,len(readn_maskedIns))
                tandem2_before = ''.join(reversed(readn_maskedIns)).find(''.join(reversed(ins)), len(readn_maskedIns) -1 -insert_start +1, len(readn_maskedIns))
# 
                # take the one closest to the insert (should be relevant only for small ITDs that may be contained multiple time within a read
                tandem2_start = None
                if tandem2_after == -1 and tandem2_before == -1:
                    tandem2_start = -1 # not found --> no itd present
                elif tandem2_after == -1:
                    tandem2_start = len(readn_maskedIns) -1 -tandem2_before -insert_length +1  # convert coords back from reverse to forward sense
                elif tandem2_before == -1:
                    tandem2_start = tandem2_after
                elif tandem2_after < tandem2_before:
                    tandem2_start = tandem2_after
                elif tandem2_before < tandem2_after:
                    tandem2_start = len(readn_maskedIns) -1 -tandem2_before -insert_length +1  # convert coords back from reverse to forward sense
                assert tandem2_start is not None  # should be assigned something!
#                    
                offset = abs(tandem2_start - insert_start)
                if trailing and offset == 0:
                    trailing = False
                    print("UNTRAIL") 
                # save if an exact second tandem of the insert was found
                if tandem2_start != -1:   # ---> also check that index of second match is sufficiently close to insert! (for exact match and alignment approach!)
                    w_itd_exact["idx"].append(i)
                    w_itd_exact["file"].append(filename)
                    w_itd_exact["length"].append(insert_length)
                    w_itd_exact["start"].append(insert_start_ref)
                    w_itd_exact["tandem2_start"].append(read_to_wt_coord(tandem2_start, refn))
                    w_itd_exact["offset"].append(offset)
                    w_itd_exact["insert"].append(''.join(ins))
                    w_itd_exact["trailing"].append(trailing)
                    #if trailing:
                    #    w_itd_exact["length"][-1] = w_itd_exact["offset"][-1]
                else:
                    # otherwise search for sufficiently similar (> 90 % bases mapped) second tandem by realignment of the insert within the remainder of the read
                    max_score = len(ins) * COST_MATCH  # param?
                    min_score = max_score * MIN_SCORE
                    # arguments: seq1, seq2, match-score, mismatch-score, gapopen-score, gapextend-score --> match/mismatch from needle default (/usr/share/EMBOSS/data/EDNAFULL), gap as passed to needle in my script
                    # output: list of optimal alignments, each a list of seq1, seq2, score, start-idx, end-idx 
                    alignments = bio.align.localcs(''.join(ins), ''.join(readn_maskedIns), get_alignment_score, COST_GAPOPEN, COST_GAPEXTEND)
                    # filter alignments where insert cannot be realigned in one piece
                    alignments = [al for al in alignments if integral_insert_realignment(al[0],insert_length)]
                    alignment_score = None
                    #
                    if alignments == []:
                        alignment_score = -1
                    else:
                        alignment = alignments[0]
                        alignment_score, alignment_start, alignment_end = alignment[2:5]
#			
                    if alignment_score >= min_score:
                        w_itd_nonexact["idx"].append(i)
                        w_itd_nonexact["file"].append(filename)
                        w_itd_nonexact["length"].append(insert_length)
                        w_itd_nonexact["start"].append(insert_start_ref)
                        w_itd_nonexact["tandem2_start"].append(read_to_wt_coord(alignment_start, refn))
                        w_itd_nonexact["offset"].append(offset)
                        w_itd_nonexact["insert"].append(''.join(ins))
                        w_itd_nonexact["trailing"].append(trailing)
                        #if trailing:
                        #    w_itd_nonexact["length"][-1] = w_itd_nonexact["offset"][-1]
                    else:
                        w_itd_nonexact_fail["idx"].append(i)
                        w_itd_nonexact_fail["file"].append(filename)
                        w_itd_nonexact_fail["length"].append(insert_length)
                        w_itd_nonexact_fail["start"].append(insert_start_ref)
                        w_itd_nonexact_fail["insert"].append(''.join(ins))
                        w_itd_nonexact_fail["trailing"].append(trailing)
                        #print(bio.format_alignment(*alignment))
                    if len(alignments) > 1:
                        ambig_i.append(i)
                        ambig_als.append(alignments)
    #
    # print number of ambiguous alignments (to see if this is sth I need to handle or not)
    print("There were {} inserts that generated ambiguous alignments.".format(len(ambig_i)))
    print("There were {} inserts whose alignment should have been left normalized.".format(should_left_normalize))
    print()
    print("There were {} trailing inserts.".format(sum(w_ins["trailing"])))
    print("There were {} trailing exact ITDs.".format(sum(w_itd_exact["trailing"])))
    print("There were {} trailing nonexact ITDs.".format(sum(w_itd_nonexact["trailing"])))
    print("There were {} trailing nonexact ITDs failed.".format(sum(w_itd_nonexact_fail["trailing"])))
    #
    #
    # fix ref_coverage -> coverage of last index is 0 since I am counting reads covering a position AND its successor -> for the final index, there is no successor but also this restraint is unnecessary (any trailing mut will be covered by any read covering the first/last base!)
    ref_coverage[-1] = ref_coverage[-2]
    ref_coverage = ref_coverage.astype(int)
    #
    #
    ########################################
    # COLLECT AND COLLAPSE ITDs
    #
    df_itd =  pd.concat([pd.DataFrame(w_itd_exact), pd.DataFrame(w_itd_nonexact)], ignore_index=True)
    df_itd["sample"] = [SAMPLE for i in range(df_itd.shape[0])]
    df_itd[["idx","length","offset","start","tandem2_start"]] = df_itd[["idx","length","offset","start","tandem2_start"]].astype("int64")
    df_itd["counts"] = [all_readCounts[i] for i in df_itd["idx"]]
    df_itd_grouped = collapse_same_inserts(df_itd, "tandem2_start", ref_wt)
    df_itd_grouped[['sample','length', 'trailing', 'tandem2_start', 'vaf', 'ref_coverage', 'counts', 'counts_each', 'file']].to_csv(os.path.join(OUT_DIR,"flt3_itds.tsv"), index=False, float_format='%.2e', sep='\t')
    #   
    #
    df_ins =  pd.DataFrame(w_ins)
    df_ins["sample"] = [SAMPLE for i in range(df_ins.shape[0])]
    df_ins["counts"] = [all_readCounts[i] for i in df_ins["idx"]]
    df_ins_grouped = collapse_same_inserts(df_ins, "start", ref_wt)
    df_ins_grouped[['sample','length', 'trailing', 'start', 'vaf', 'ref_coverage', 'counts', 'counts_each', 'file']].to_csv(os.path.join(OUT_DIR,"flt3_ins.tsv"), index=False, float_format='%.2e', sep='\t')
    #
    #
    #
    ########################################
    # COLLAPSE ITDs
    # --> align inserts of same length and tandem2_start, collapse if they are sufficiently similar
    # --> align inserts of same length,  collapse if they are sufficiently similar
    # --> filter inserts based on number of unique and total supporting reads
    #
    # collapse, save, filter and save filtered ITDs
    # --> instead of indexing all the same, can I index once and discard all other columns?
    df_itd_collapsed = collapse_similar_inserts(df_itd_grouped, "tandem2_start").sort_values(['length','tandem2_start'])
    fix_trailing_length(df_itd_collapsed)[['sample','length', 'trailing', 'tandem2_start', 'vaf', 'ref_coverage', 'counts', 'insert']].to_csv(os.path.join(OUT_DIR,"flt3_itds_collapsed.tsv"), index=False, float_format='%.2e', sep='\t')
    df_itd_collapsed = collapse_close_inserts(df_itd_collapsed, "tandem2_start").sort_values(['length','tandem2_start'])
    fix_trailing_length(df_itd_collapsed)[['sample','length', 'trailing', 'tandem2_start', 'vaf', 'ref_coverage', 'counts', 'insert']].to_csv(os.path.join(OUT_DIR,"flt3_itds_collapsed_full.tsv"), index=False, float_format='%.2e', sep='\t')
    if 'cr' not in SAMPLE: # change this to some binary flag
        df_itd_collapsed = filter_inserts(df_itd_collapsed).sort_values(['length','tandem2_start'])
        fix_trailing_length(df_itd_collapsed)[['sample','length', 'trailing', 'tandem2_start', 'vaf', 'ref_coverage', 'counts', 'insert']].to_csv(os.path.join(OUT_DIR,"flt3_itds_collapsed_full_filtered.tsv"), index=False, float_format='%.2e', sep='\t')
    #
    df_ins_collapsed = collapse_similar_inserts(df_ins_grouped, "start").sort_values(['length','start'])
    df_ins_collapsed[['sample','length', 'start', 'vaf', 'ref_coverage', 'counts', 'insert']].to_csv(os.path.join(OUT_DIR,"flt3_ins_collapsed.tsv"), index=False, float_format='%.2e', sep='\t')
    df_ins_collapsed = collapse_close_inserts(df_ins_collapsed, "start").sort_values(['length','start'])
    df_ins_collapsed[['sample','length', 'start', 'vaf', 'ref_coverage', 'counts', 'insert']].to_csv(os.path.join(OUT_DIR,"flt3_ins_collapsed_full.tsv"), index=False, float_format='%.2e', sep='\t')
    if 'cr' not in SAMPLE: # change this to some binary flag
        df_ins_collapsed = filter_inserts(df_ins_collapsed).sort_values(['length','start'])
        df_ins_collapsed[['sample','length', 'start', 'vaf', 'ref_coverage', 'counts', 'insert']].to_csv(os.path.join(OUT_DIR,"flt3_ins_collapsed_full_filtered.tsv"), index=False, float_format='%.2e', sep='\t')
    #
    #
    #
    ########################################
    # PRINT SUMMARY STATISTICS on the number of reads in each category
    #
    print("\nNumber of unique reads supporting each type of insert")
    print("Insertions: {}".format(len(w_ins["idx"])))
    print("Single exact ITD: {}".format(len(w_itd_exact["idx"])))
    print("Single non-exact ITD: {}".format(len(w_itd_nonexact["idx"])))
    print("Single insertion failed alignment: {}".format(len(w_itd_nonexact_fail["idx"])))
    assert len(w_ins["idx"]) == len(w_itd_exact["idx"]) + len(w_itd_nonexact["idx"]) + len(w_itd_nonexact_fail["idx"])
    #
    #
    #
    ########################################
    # GET KNOWN ITDs 
    # --> if length known is given, extract relevant ITDs 
    # --> if vaf known is given, compare numbers? (vaf is always known if length is known? is there a better way to supply this together?)
    #
    if KNOWN_LENGTH_FILE is not None:
        known_length = read_known(KNOWN_LENGTH_FILE,int)
        df_itd_known=None
        df_ins_known=None
        # define output file names here also! -> keep names consistent for cr samples
        df_itd_known = get_known(fix_trailing_length(df_itd_collapsed), known_length)
        df_ins_known = get_known(df_ins_collapsed, known_length)
        #
        # print tables with known ITDs only -> useful to check concordance of different experiments
        df_itd_known[['sample','length','vaf','ref_coverage','counts','tandem2_start','insert']].to_csv(os.path.join(OUT_DIR,"flt3_itds_collapsed_full_filtered_known.tsv"), index=False, float_format='%.2e', sep='\t', na_rep='NA')
        df_ins_known[['sample','length','vaf','ref_coverage','counts','start','insert']].to_csv(os.path.join(OUT_DIR,"flt3_ins_collapsed_full_filtered_known.tsv"), index=False, float_format='%.2e', sep='\t', na_rep='NA')
        #
        # associate detected ITDs with expected VAF (if available)  -> useful to check correlation between VAF estimates of different experiments --> right now assuming there is only one VAF/AR for sum of all ITD clones!
        known_vaf = None
        if KNOWN_VAF_FILE is not None:
            known_vaf = read_known(KNOWN_VAF_FILE,float)[0]
        elif KNOWN_AR_FILE is not None:
            known_vaf = ar_to_vaf(read_known(KNOWN_AR_FILE,float)[0])
        #
        if known_vaf is not None:
            assert known_vaf <= 100 and known_vaf >= 0
        #
        # does this make sense with multiple inserts per read? counts/vaf would be messed up because counted twice, right? --> more accurate maybe: collect all supporting reads and count unique 
        df_itd_known_collapsed = collapse_all(df_itd_known,keep=["sample"],add=["counts","vaf"],append=["length","tandem2_start","ref_coverage"])
        df_itd_known_collapsed["vaf_genescan"] = known_vaf
        # 
        df_ins_known_collapsed = collapse_all(df_ins_known,keep=["sample"],add=["counts","vaf"],append=["length","start","ref_coverage"])
        df_ins_known_collapsed["vaf_genescan"] = known_vaf
        #
        # print collapsed tables with known ITDs only -> useful to check concordance with GeneScan VAF
        df_itd_known_collapsed[['sample','length','vaf','vaf_genescan','vaf_each','tandem2_start','ref_coverage','counts_each']].to_csv(os.path.join(OUT_DIR,"flt3_itds_collapsed_full_filtered_known_collapsed.tsv"), index=False, float_format='%.2e', sep='\t', na_rep='NA')
        df_ins_known_collapsed[['sample','length','vaf','vaf_genescan','vaf_each','start','ref_coverage','counts_each']].to_csv(os.path.join(OUT_DIR,"flt3_ins_collapsed_full_filtered_known_collapsed.tsv"), index=False, float_format='%.2e', sep='\t', na_rep='NA')
        

        
        

