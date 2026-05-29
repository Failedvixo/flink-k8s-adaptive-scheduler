package com.thesis.benchmark.nexmark;

import java.io.Serializable;

/**
 * Tagged-union event wrapper.
 * The source emits one stream of NexmarkEvent and downstream operators
 * filter by {@link #type}. This matches how Beam Nexmark structures its
 * generator: a single timeline of interleaved person/auction/bid events
 * so cross-stream joins (Q3, Q8) see realistic interleaving.
 */
public class NexmarkEvent implements Serializable {
    public enum Type { PERSON, AUCTION, BID }

    public Type type;
    public Person person;
    public Auction auction;
    public Bid bid;
    public long eventTime;   // copy of inner event time for watermarks/keying

    public NexmarkEvent() {}

    public static NexmarkEvent of(Person p) {
        NexmarkEvent e = new NexmarkEvent();
        e.type = Type.PERSON;
        e.person = p;
        e.eventTime = p.dateTime;
        return e;
    }

    public static NexmarkEvent of(Auction a) {
        NexmarkEvent e = new NexmarkEvent();
        e.type = Type.AUCTION;
        e.auction = a;
        e.eventTime = a.dateTime;
        return e;
    }

    public static NexmarkEvent of(Bid b) {
        NexmarkEvent e = new NexmarkEvent();
        e.type = Type.BID;
        e.bid = b;
        e.eventTime = b.dateTime;
        return e;
    }
}
