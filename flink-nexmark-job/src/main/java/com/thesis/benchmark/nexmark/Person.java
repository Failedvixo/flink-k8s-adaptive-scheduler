package com.thesis.benchmark.nexmark;

import java.io.Serializable;

/**
 * Nexmark Person event (canonical schema).
 * Public no-arg ctor + public fields → Flink POJO serializer.
 */
public class Person implements Serializable {
    public long id;
    public String name;
    public String emailAddress;
    public String city;
    public String state;
    public long dateTime;   // event-time millis since epoch

    public Person() {}

    public Person(long id, String name, String emailAddress,
                  String city, String state, long dateTime) {
        this.id = id;
        this.name = name;
        this.emailAddress = emailAddress;
        this.city = city;
        this.state = state;
        this.dateTime = dateTime;
    }
}
