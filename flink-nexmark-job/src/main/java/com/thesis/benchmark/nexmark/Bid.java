package com.thesis.benchmark.nexmark;

import java.io.Serializable;

/** Nexmark Bid event (canonical schema). */
public class Bid implements Serializable {
    public long auction;      // auction id
    public long bidder;       // person id
    public long price;
    public String channel;
    public long dateTime;     // event-time millis

    public Bid() {}

    public Bid(long auction, long bidder, long price, String channel, long dateTime) {
        this.auction = auction;
        this.bidder = bidder;
        this.price = price;
        this.channel = channel;
        this.dateTime = dateTime;
    }
}
