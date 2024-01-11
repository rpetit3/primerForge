import multiprocessing
from Bio.Seq import Seq
from bin.Primer import Primer
from Bio.SeqRecord import SeqRecord
from multiprocessing.managers import ListProxy
from bin.utilities import getAllKmers, getUniqueKmers, kmpSearch


def __getSharedKmers(seqs:dict[str, list[SeqRecord]], minLen:int, maxLen:int) -> dict[str, dict[Seq, tuple[str,int,int]]]:
    """retrieves all the kmers that are shared between the input genomes

    Args:
        seqs (dict[str, list[SeqRecord]]): key = genome name; val = list of contigs as SeqRecord
        minLen (int): the minimum primer length
        maxLen (int): the maximum primer length

    Returns:
        dict[str, dict[Seq, tuple[str,int,int]]]: key = genome name; val = dict: key = kmer sequence; val = contig, start, length
    """
    # intialize variables
    sharedKmers = set()
    kmers = dict()
    
    # for each genome
    for name in seqs.keys():
        # get the unique kmers
        kmers[name] = getUniqueKmers(seqs[name], minLen, maxLen)
        
        # keep all the kmers if this is the first iteration
        if sharedKmers == set():
            sharedKmers.update(set(kmers[name].keys()))
        
        # otherwise keep only the shared kmers
        else:
            sharedKmers.intersection_update(kmers[name].keys())

    # for each genome
    for name in kmers.keys():
        # get the set of kmers that are not shared
        bad = set(kmers[name].keys()).difference(sharedKmers)
        
        # remove all the unshared kmers from the dictionary
        for kmer in bad:
            kmers[name].pop(kmer)
    
    return kmers


def __reorganizeDataByPosition(kmers:dict[str, dict[Seq, tuple[str, int, int]]]) -> dict[str, dict[str, dict[int, list[tuple[Seq, int]]]]]:
    """reorganizes data from __getSharedKmers by its genomic position

    Args:
        kmers (dict[str, dict[Seq, tuple[str, int, int]]]): key=contig; val=dict: key=sequence; val=contig,start,length

    Returns:
        dict[str, dict[str, dict[int, list[tuple[Seq, int]]]]]: key=contig; val=dict: key=start position; val=list of seq,length tuples
    """
    # initialize output
    out = dict()
    
    # for each kmer
    for kmer in kmers.keys():
        # extract data from the dictionary
        contig, start, length = kmers[kmer]
        
        # name = top level key; contig = second level key; start position = third level key; val = list
        out[contig] = out.get(contig, dict())
        out[contig][start] = out[contig].get(start, list())
        
        # add the sequence and its length to the list
        out[contig][start].append((kmer, length))
    
    return out


def __evaluateAllKmers(kmersD:dict[str, dict[int, list[tuple[Seq,int]]]], minGc:float, maxGc:float, minTm:float, maxTm:float, numThreads:int) -> list[Primer]:
    """evaluates all the kmers for their suitability as primers

    Args:
        kmersD (dict[str, dict[int, list[tuple[Seq,int]]]]): the dictionary produced by __reorganizeDataByPosition
        minGc (float): the minimum percent G+C
        maxGc (float): the maximum percent G+C
        minTm (float): the minimum melting temperature
        maxTm (float): the maximum melting temperature
        numThreads (int): number of threads for parallel processing

    Returns:
        list[Primer]: a list of suitable primers as Primer objects
    """
    # initialize a shared list and a list of arguments
    primerL = multiprocessing.Manager().list()
    argsL = list()

    # each contig needs to be evalutated
    for contig in kmersD.keys():
        # each start position within the contig needs to be evaluated
        for start in kmersD[contig].keys():
            # save arguments to pass in parallel
            argsL.append((contig, start, kmersD[contig][start], minGc, maxGc, minTm, maxTm, primerL))

    # parallelize primer evaluations
    pool = multiprocessing.Pool(processes=numThreads)
    pool.starmap(__evaluateKmersAtOnePosition, argsL)
    pool.close()
    pool.join()

    # collapse the shared list before returning
    return list(primerL)
 

def __evaluateKmersAtOnePosition(contig:str, start:int, posL:list[tuple[Seq,int]], minGc:float, maxGc:float, minTm:float, maxTm:float, shareL:ListProxy) -> None:
    """evaluates all the primers at a single position in the genome; designed for parallel calls

    Args:
        contig (str): the name of the contig
        start (int): the start position in the sequence
        posL (list[tuple[Seq,int]]): a list of tuples; primer sequence and primer length
        minGc (float): the minimum percent GC allowed
        maxGc (float): the maximum percent GC allowed
        minTm (float): the minimum melting temperature allowed
        maxTm (float): the maximum melting temperature allowed
        shareL (ListProxy): a shared list for parallel calling;
    
    Returns:
        Does not return.
        Primer sequences passing the boolean checks are added to the shared list
    """
    # define helper functions to make booleans below more readable
    def isGcWithinRange(primer:Primer) -> bool:
        """is the percent GC within the acceptable range?"""
        return primer.gcPer >= minGc and primer.gcPer <= maxGc

    def isTmWithinRange(primer:Primer) -> bool:
        """is the Tm within the acceptable range?"""
        return primer.Tm >= minTm and primer.Tm <= maxTm
    
    def noLongRepeats(primer:Primer) -> bool:
        """verifies that a primer does not have long repeats
            O(1)
        """
        # constants
        MAX_LEN = 4
        REPEATS = ("A"*MAX_LEN, "T"*MAX_LEN, "C"*MAX_LEN, "G"*MAX_LEN)

        # check each repeat in the primer
        for repeat in REPEATS:
            if kmpSearch(primer.seq, repeat):
                return False
        return True

    def noIntraPrimerComplements(primer:Primer) -> bool:
        """verifies that the primer does not have hairpin potential
            O(len(primer))!
        """
        # constants
        MAX_LEN = 3
        LEN_TO_CHECK = MAX_LEN + 1
        
        # go through each frame of the primer up to the max length
        for idx in range(len(primer)-MAX_LEN):
            # get the fragment and see if the reverse complement exists downstream
            revComp = primer.seq.reverse_complement()
            if kmpSearch(primer.seq, revComp[idx:idx+LEN_TO_CHECK]):
                return False
        
        return True
    
    # initialize values for the while loop
    found = False
    idx = 0
    
    # continue to iterate through each primer in the list until a primer is found 
    while idx < len(posL) and not found:
        # extract data from the list
        seq,length = posL[idx]
        
        # create a Primer object
        primer = Primer(seq, contig, start, length)
        
        # evaluate the primer's percent GC, Tm, and homology; save if found
        if isGcWithinRange(primer) and isTmWithinRange(primer): # O(1)
            if noLongRepeats(primer): # O(1)
                if noIntraPrimerComplements(primer): # this runtime is the worst O(len(primer)); evaluate last
                    shareL.append(primer)
                    found = True
                
        # move to the next item in the list
        idx += 1


def __buildOutput(kmers:dict[str, dict[Seq, tuple[str,int,int]]], candidates:list[Primer]) -> dict[str, dict[str, list[Primer]]]:
    """builds the candidate primer output

    Args:
        kmers (dict[str, dict[Seq, tuple[str,int,int]]]): the dictionary produced by __getSharedKmers
        candidates (list[Primer]): the list produced by __evaluateAllKmers

    Returns:
        dict[str, dict[str, list[Primer]]]: key=genome name; val=dict: key=contig; val=list of Primers
    """
    # initialize output
    out = dict()
    
    # for each genome
    for name in kmers:
        out[name] = dict()
        
        # for each candidate primer
        for cand in candidates:
            # extract the data for this candidate, create a Primer; add it to the list
            contig, start, length = kmers[name][cand.seq]
            out[name][contig] = out[name].get(contig, list())
            out[name][contig].append(Primer(cand.seq, contig, start, length))
    
    # sort lists of primers by start their position
    for name in out.keys():
        for contig in out[name].keys():
            out[name][contig] = sorted(out[name][contig], key=lambda x: x.start)
    
    return out


def getAllCandidatePrimers(ingroup:dict[str, list[SeqRecord]], outgroup:dict[str, list[str]], minLen:int, maxLen:int, minGc:float, maxGc:float, minTim:float, maxTm:float, numThreads:int) -> dict[str, dict[str, list[Primer]]]:
    """gets all the candidate primer sequences for a given ingroup with respect to a given outgroup

    Args:
        ingroup (dict[str, list[SeqRecord]]): the ingroup sequences: key=genome name; val=contigs as SeqRecords
        outgroup (dict[str, list[str]]): the outgroup sequences: key=genome name; val=contigs as SeqRecords
        minLen (int): minimum primer length
        maxLen (int): maximum primer length
        minGc (float): minimum primer G+C percent
        maxGc (float): maximum primer G+C percent
        minTim (float): minimum primer melting temp
        maxTm (float): maximum primer melting temp
        numThreads (int): number threads available for parallel processing

    Returns:
        dict[str, dict[str, list[Primer]]]: key=genome name; val=dict: key=contig; val=list of Primers
    """
    # get all non-duplicated kmers that are shared in the ingroup
    ingroupKmers = __getSharedKmers(ingroup, minLen, maxLen)
    
    # get all the kmers in the outgroup
    outgroupKmers = set()
    for name in outgroup:
        for rec in outgroup[name]:
            outgroupKmers.update(getAllKmers(rec, minLen, maxLen))

    # remove any outgroup kmers from the ingroup kmers
    for name in ingroupKmers.keys():
        for seq in set(ingroupKmers[name].keys()):
            if seq in outgroupKmers:
                ingroupKmers[name].pop(seq)
    
    # reorganize data by each unique start positions for one genome
    positions = __reorganizeDataByPosition(next(iter(ingroupKmers.values())))
    
    # get a list of the kmers that pass the evaulation
    candidates = __evaluateAllKmers(positions, minGc, maxGc, minTim, maxTm, numThreads)
    
    # create a dictionary whose keys are contigs and values are the primers
    return __buildOutput(ingroupKmers, candidates)
